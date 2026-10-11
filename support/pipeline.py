"""One turn of the support desk, as a stream of events (SPEC P-1, section 7.1).

The CLI and (later) the web server only DISPLAY what this module yields.
Stage 5: the whole turn runs inside ONE root span called `agent.turn` (O-2), so the
agent's own spans (invoke_agent, call_llm, execute_tool) hang underneath it as children.
Stage 6: two guards run BEFORE the agent, in this order: sanitize, then judge (P-2).
Stage 7: a third guard, the Guardrail, runs after the Judge (GR-1 to GR-4).
Stage 8: three more steps, so the order is now complete (P-2):
    sanitize -> judge -> guardrail -> recall -> agent -> mask -> save
  recall  reads this customer's memories from Mem0 and puts the good ones above the message.
  mask    sends the agent's reply to the Masker service, which hides personal data.
  save    stores the customer's message (never the reply) in Mem0.
A guard that blocks ends the turn (P-3): no recall, no agent, no save.
A step that fails ends the turn with an error; nothing is let through "just in case" (P-4).
Stage 9: the `tool_call` event now also carries the tool's SQL and parameter names (7.1).
"""

import asyncio
import json
import secrets
import time
from pathlib import Path

from google.genai import types
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.trace import Status, StatusCode

from guards.guardrail import check_guardrail
from guards.judge_client import ask_judge
from guards.masker_client import ask_masker
from guards.sanitizer import sanitize
from support.memory import build_agent_input, recall, save
from support.telemetry import get_tracer, trace_url
from support.toolinfo import tool_info
from support.toolresult import decode_args, only_own_rows, read_tool_response

RUNS_DIR = Path("runs")
# Per-turn budgets (P-6). The numbers are defined in THRESHOLDS.md as T-BUD-TOOLS,
# T-BUD-TOKENS and T-BUD-WALL. If they change there, change them here.
MAX_TOOL_CALLS = 6
MAX_TOKENS = 30_000
MAX_WALL_S = 30
# The mask and save steps run AFTER the agent, so the agent must stop early enough to leave
# them time. Their own timeouts (Masker 2 s + Mem0 save 4 s) add up to this reserve, which
# keeps the whole turn inside MAX_WALL_S.
AFTER_AGENT_RESERVE_S = 6
AGENT_DEADLINE_S = MAX_WALL_S - AFTER_AGENT_RESERVE_S
ACCESS = {"action-log": "WRITE"}  # every other tool only reads
# What the customer sees when the Sanitizer or the Judge blocks. It is the same for both on
# purpose: it must not teach an attacker which rule caught them. The real reason is in the
# step event.
REFUSAL = ("Sorry, I can't help with that request. "
           "I can help with your orders, deliveries, returns and account.")
# Guardrail blocks are usually off-topic customers, not attackers, so they get one extra
# line of help (Stage 7 decision). Sanitizer and Judge blocks keep the plain message.
GUARDRAIL_REFUSAL = REFUSAL + " If this is about an order, please tell me the order number."
NO_TOKENS = {"in": 0, "out": 0}


async def _check_sanitize(message: str):
    """Guard 1 (S-1): plain Python, no model call. Returns (ok, detail, tokens)."""
    result = sanitize(message)
    return result.ok, result.detail, NO_TOKENS


async def _check_judge(message: str):
    """Guard 2 (J-4): ask the Judge service over A2A. Raises JudgeError if there is no verdict."""
    answer = await ask_judge(message)
    detail = f"{answer['verdict']}: {answer['reason']} (decided by {answer['source']})"
    return answer["verdict"] == "allow", detail, answer["tokens"]


async def _check_guardrail(message: str):
    """Guard 3 (GR-1): in-process agent. Raises GuardrailError if there is no clear decision."""
    answer = await check_guardrail(message)
    return answer["decision"] == "safe", f"{answer['decision']}: {answer['reasoning']}", answer["tokens"]


# The guards in P-2 order: (step key, label, span name, kind shown in the event, check function).
GUARDS = [
    ("sanitize", "Sanitizer", "security.sanitize", "Python fn", _check_sanitize),
    ("judge", "A2A Security Judge", "security.a2a_judge", "A2A", _check_judge),
    ("guardrail", "Guardrail", "guardrail.check", "in-process", _check_guardrail),
]


async def _run_guard(span_name: str, check, message: str) -> dict:
    """Run one guard inside its own span. Returns status "passed", "blocked" or "error".

    The span is opened and closed in here, with no `yield` in between, so it always ends up
    as a child of `agent.turn` and is closed before the next step starts.
    """
    start = time.monotonic()
    with get_tracer().start_as_current_span(span_name) as span:
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.GUARDRAIL.value)
        span.set_attribute(SpanAttributes.INPUT_VALUE, message)
        try:
            ok, detail, tokens = await check(message)
        except Exception as exc:  # the guard could not decide: that is an ERROR, never a pass (P-4)
            # EVALS section 6: the error must say the request could not be checked.
            why = f"{str(exc) or type(exc).__name__}. The request could not be checked, so it was not answered."
            span.record_exception(exc)                       # the exception shows on the red span
            span.set_status(Status(StatusCode.ERROR, why))
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, why)
            return {"status": "error", "detail": why, "ms": _ms(start), "tokens": NO_TOKENS, "exc": exc}
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, detail)
        span.set_attribute("guard.blocked", not ok)
        return {"status": "passed" if ok else "blocked", "detail": detail, "ms": _ms(start), "tokens": tokens}


async def _run_step(span_name: str, span_kind, input_value: str, work, if_it_fails: str) -> dict:
    """Run one Stage 8 step (recall, mask or save) inside its own span.

    `work(span)` does the real job and returns a dict that must contain "detail" and "output".
    Returns that dict plus status "passed", or status "error" if `work` raised.
    Like _run_guard, there is no `yield` in here, so the span is a child of `agent.turn`.
    """
    start = time.monotonic()
    with get_tracer().start_as_current_span(span_name) as span:
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, span_kind.value)
        span.set_attribute(SpanAttributes.INPUT_VALUE, input_value)
        try:
            result = await work(span)
        except Exception as exc:  # fail loud: the turn ends with an error naming this step (P-4)
            why = f"{str(exc) or type(exc).__name__}. {if_it_fails}"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, why))
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, why)
            return {"status": "error", "detail": why, "ms": _ms(start), "exc": exc}
        span.set_attribute(SpanAttributes.OUTPUT_VALUE, result["output"])
        return {"status": "passed", "ms": _ms(start), **result}


async def _do_recall(message: str, user_email: str) -> dict:
    """Step 4 (R-1, R-2, R-5): search Mem0. Span `memory.recall`, kind RETRIEVER."""
    async def work(span) -> dict:
        memories = await recall(message, user_email)
        # SPEC section 10: each candidate is a "document" with its content and score.
        for i, m in enumerate(memories):
            prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{i}.document"
            span.set_attribute(f"{prefix}.content", m["memory"])
            span.set_attribute(f"{prefix}.score", m["score"])
            span.set_attribute(f"{prefix}.metadata", json.dumps({"inserted": m["inserted"], "reason": m["reason"]}))
        inserted = sum(1 for m in memories if m["inserted"])
        skipped = len(memories) - inserted
        if memories:
            detail = f"{len(memories)} memories found: {inserted} inserted, {skipped} skipped"
        else:
            detail = "no memories found for this customer"
        span.set_attribute("memory.inserted", inserted)
        span.set_attribute("memory.skipped", skipped)
        return {"detail": detail, "output": detail, "memories": memories, "inserted": inserted, "skipped": skipped}

    return await _run_step("memory.recall", OpenInferenceSpanKindValues.RETRIEVER, message, work,
                           "Memory could not be read, so the request was not answered.")


async def _do_mask(reply: str, user_email: str) -> dict:
    """Step 6 (K-1 to K-3): ask the Masker service over A2A. Span `security.a2a_mask`, kind GUARDRAIL.

    The span's input is the reply before masking and its output is the reply after masking.
    """
    async def work(span) -> dict:
        answer = await ask_masker(reply, user_email)
        span.set_attribute("mask.total", answer["total"])
        span.set_attribute("mask.detail", answer["detail"])
        return {"detail": answer["detail"], "output": answer["masked_text"],
                "masked_text": answer["masked_text"], "total": answer["total"]}

    return await _run_step("security.a2a_mask", OpenInferenceSpanKindValues.GUARDRAIL, reply, work,
                           "The reply could not be checked for personal data, so it was not sent.")


async def _do_save(message: str, user_email: str) -> dict:
    """Step 7 (R-4): store the USER'S MESSAGE in Mem0. Span `memory.save`, kind TOOL.

    This function is only ever given `message`. The agent's reply never reaches it (section 11, R-5).
    """
    async def work(span) -> dict:
        span.set_attribute(SpanAttributes.TOOL_NAME, "mem0.add")
        answer = await save(message, user_email)
        detail = f"user message sent to Mem0 (status {answer['status']})"
        return {"detail": detail, "output": detail}

    return await _run_step("memory.save", OpenInferenceSpanKindValues.TOOL, message, work,
                           "The message could not be saved to memory, so the turn was stopped.")


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _text(event) -> str:
    parts = event.content.parts if event.content and event.content.parts else []
    return "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))


def _write_log(log: dict) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / f"{log['turn_id']}.json").write_text(json.dumps(log, indent=2, default=str))


def _fail(root, log: dict, key: str, step: dict, t0: float, tokens: dict) -> dict:
    """A Stage 8 step failed: finish the run log and the root span, and return the `error` event.

    The run log is written in here, so it is on disk BEFORE the caller yields the event (P-5).
    """
    log["steps"].append({"key": key, "status": "error", "ms": step["ms"], "detail": step["detail"]})
    log.update(terminated="error", wall_clock_ms=_ms(t0), tokens=tokens, error=step["detail"])
    root.set_attribute("turn.terminated", "error")
    root.set_attribute(SpanAttributes.OUTPUT_VALUE, step["detail"])
    root.record_exception(step["exc"])
    root.set_status(Status(StatusCode.ERROR, step["detail"]))
    _write_log(log)
    return {"type": "error", "step": key, "status": 502, "error": step["detail"], "terminated": "error"}


async def run_turn(runner, user_email: str, session_id: str, message: str):
    """Run one customer message and yield events. Always ends with exactly one `final` or `error`."""
    turn_id = f"turn_{int(time.time() * 1000):x}{secrets.token_hex(3)}"
    agent = runner.agent
    t0 = time.monotonic()

    # The span is opened INSIDE this generator, and the runner is iterated inside the `with`
    # block, in the same task. That is what makes `invoke_agent` a CHILD of `agent.turn`
    # instead of its own separate trace (SPEC section 10, TECHNICAL troubleshooting).
    with get_tracer().start_as_current_span("agent.turn") as root:
        root.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.CHAIN.value)
        root.set_attribute(SpanAttributes.INPUT_VALUE, message)       # Option A: full text recorded
        root.set_attribute(SpanAttributes.USER_ID, user_email)
        root.set_attribute(SpanAttributes.SESSION_ID, session_id)
        root.set_attribute("turn.id", turn_id)
        trace_id = format(root.get_span_context().trace_id, "032x")

        log = {
            "turn_id": turn_id, "trace_id": trace_id, "user": user_email, "message": message,
            "terminated": None, "blocked_at": None, "wall_clock_ms": 0, "tokens": {"in": 0, "out": 0},
            "steps": [], "tool_calls": [], "llm_calls": 0,
        }
        yield {"type": "trace", "turn_id": turn_id, "trace_id": trace_id, "url": trace_url(trace_id)}
        tokens_in = tokens_out = 0  # the whole turn's tokens: the guards' model calls count too (P-6)

        # ---- Steps 1 to 3: the guards. Each one must pass before anything after it may run. ----
        for key, label, span_name, kind, check in GUARDS:
            yield {"type": "stage", "key": key, "label": label}
            guard = await _run_guard(span_name, check, message)
            tokens_in, tokens_out = tokens_in + guard["tokens"]["in"], tokens_out + guard["tokens"]["out"]
            log["steps"].append({"key": key, "status": guard["status"], "ms": guard["ms"], "detail": guard["detail"]})
            if guard["status"] == "passed":
                yield {"type": "step", "key": key, "status": "passed", "detail": guard["detail"],
                       "ms": guard["ms"], "span": span_name, "kind": kind}
                continue

            # The turn ends here. The agent never sees the message, so there is no invoke_agent span.
            # Nothing below this loop runs either, so a blocked message is never saved (P-3).
            wall = _ms(t0)
            terminated = "error" if guard["status"] == "error" else "blocked"
            log.update(terminated=terminated, wall_clock_ms=wall, tokens={"in": tokens_in, "out": tokens_out})
            root.set_attribute("turn.terminated", terminated)
            if terminated == "error":
                root.set_attribute(SpanAttributes.OUTPUT_VALUE, guard["detail"])
                root.record_exception(guard["exc"])
                root.set_status(Status(StatusCode.ERROR, guard["detail"]))
                log["error"] = guard["detail"]
                _write_log(log)  # before the terminal event (P-5)
                yield {"type": "error", "step": key, "status": 502, "error": guard["detail"], "terminated": "error"}
                return
            log["blocked_at"] = key
            refusal = GUARDRAIL_REFUSAL if key == "guardrail" else REFUSAL
            root.set_attribute("turn.blocked_at", key)
            root.set_attribute(SpanAttributes.OUTPUT_VALUE, refusal)
            _write_log(log)
            yield {"type": "step", "key": key, "status": "blocked", "detail": guard["detail"],
                   "ms": guard["ms"], "span": span_name, "kind": kind}
            yield {"type": "final", "blocked": True, "blocked_at": key, "response": refusal,
                   "terminated": "blocked", "wall_clock_ms": wall, "tokens": {"in": tokens_in, "out": tokens_out}}
            return

        # ---- Step 4: recall. Pipeline code does this on EVERY turn; the model is not asked (R-1). ----
        yield {"type": "stage", "key": "recall", "label": "Memory recall (Mem0)"}
        recalled = await _do_recall(message, user_email)
        if recalled["status"] == "error":
            yield _fail(root, log, "recall", recalled, t0, {"in": tokens_in, "out": tokens_out})
            return
        log["steps"].append({"key": "recall", "status": "passed", "ms": recalled["ms"],
                             "inserted": recalled["inserted"], "skipped": recalled["skipped"]})
        yield {"type": "step", "key": "recall", "status": "passed", "detail": recalled["detail"],
               "ms": recalled["ms"], "span": "memory.recall", "kind": "Python fn",
               "memories": recalled["memories"]}  # R-5: every candidate, with score and inserted or not
        # R-3: the inserted memories go above the customer's message, under a fixed header.
        agent_input = build_agent_input(message, recalled["memories"])

        # ---- Step 5: the support agent. ----
        yield {"type": "stage", "key": "agent", "label": "Support agent"}

        agent_t0 = time.monotonic()
        pending = []  # tool calls still waiting for their result
        mark = time.monotonic()  # when the last thing finished, so we can time the next model call
        reply, terminated, error, cap_reason = "", "done", None, None

        new_message = types.Content(role="user", parts=[types.Part(text=agent_input)])
        events = runner.run_async(user_id=user_email, session_id=session_id, new_message=new_message)
        try:
            while True:
                left = AGENT_DEADLINE_S - (time.monotonic() - t0)
                if left <= 0:
                    terminated, cap_reason = "cap", "time"
                    break
                try:
                    event = await asyncio.wait_for(events.__anext__(), left)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    if time.monotonic() - t0 < AGENT_DEADLINE_S - 0.1:
                        raise  # a real timeout from inside the model or tool, not our budget
                    terminated, cap_reason = "cap", "time"
                    break

                now = time.monotonic()
                usage = event.usage_metadata
                t_in = (usage.prompt_token_count or 0) if usage else 0
                # "Out" = the visible answer PLUS the model's thinking tokens. The model produces
                # (and is billed for) both, and the token budget must count them (P-6, T-BUD-TOKENS).
                t_out = (
                    (usage.candidates_token_count or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                    if usage else 0
                )
                tokens_in, tokens_out = tokens_in + t_in, tokens_out + t_out
                calls, responses = event.get_function_calls(), event.get_function_responses()
                text = _text(event) if event.is_final_response() else ""

                if calls or text:  # the model just answered: report that model call
                    log["llm_calls"] += 1
                    yield {
                        "type": "llm", "model": str(getattr(agent, "model", "unknown")),
                        "decision": "call " + ", ".join(c.name for c in calls) if calls else "final answer",
                        "tokens_in": t_in, "tokens_out": t_out, "ms": int((now - mark) * 1000),
                        "span": "call_llm",
                    }
                    mark = now

                for call in calls:
                    if len(log["tool_calls"]) >= MAX_TOOL_CALLS:
                        terminated, cap_reason = "cap", "tool-call"
                        break
                    # Stage 9: the SQL this tool runs and its parameter names, read from
                    # tools.yaml, so a screen can show the query with values filled in (7.1).
                    sql = tool_info(call.name)
                    entry = {"name": call.name, "ok": None, "ms": 0}
                    call_id = len(log["tool_calls"]) + 1
                    log["tool_calls"].append(entry)
                    pending.append({"id": call_id, "adk_id": call.id, "name": call.name, "start": now, "entry": entry})
                    yield {
                        "type": "tool_call", "id": call_id, "name": call.name, "args": decode_args(dict(call.args or {})),
                        "info": {"kind": "MCP", "access": ACCESS.get(call.name, "READ"),
                                 "statement": sql["statement"], "params": sql["params"]},
                        "span": f"execute_tool {call.name}",
                    }
                if terminated == "cap":
                    break

                for resp in responses:
                    match = next((p for p in pending if resp.id and p["adk_id"] == resp.id), None) or next(
                        (p for p in pending if p["name"] == resp.name), None)
                    if match is None:
                        raise RuntimeError(f"tool result for {resp.name} has no matching tool call")
                    pending.remove(match)
                    ok, result, err = read_tool_response(resp.response)
                    dropped = 0
                    if ok:
                        result, dropped = only_own_rows(result, user_email)
                    ms = int((now - match["start"]) * 1000)
                    match["entry"].update(ok=ok, ms=ms)
                    if not ok:
                        match["entry"]["error"] = err
                    out = {"type": "tool_result", "id": match["id"], "name": resp.name, "ok": ok, "ms": ms}
                    out.update({"result": result} if ok else {"error": err})
                    if dropped:
                        out["withheld_rows"] = dropped  # T-LEAK safety net fired; see only_own_rows
                    yield out
                    mark = now

                if text:
                    reply = text
                if tokens_in + tokens_out > MAX_TOKENS:
                    terminated, cap_reason = "cap", "token"
                    break
        except Exception as exc:  # Fail loud: report it, never pretend the turn worked
            terminated, error = "error", f"{type(exc).__name__}: {exc}"
            root.record_exception(exc)  # the red span in Phoenix carries the exception
        finally:
            await events.aclose()

        if terminated == "done" and not reply:
            terminated, error = "error", "the agent finished without a reply"

        agent_ms = _ms(agent_t0)  # the agent step on its own

        if terminated == "error":
            log.update(terminated=terminated, wall_clock_ms=_ms(t0), tokens={"in": tokens_in, "out": tokens_out})
            root.set_attribute("turn.terminated", terminated)
            root.set_attribute(SpanAttributes.OUTPUT_VALUE, error)
            root.set_status(Status(StatusCode.ERROR, error))  # marks the span red in Phoenix
            log["steps"].append({"key": "agent", "status": "error", "ms": agent_ms})
            log["error"] = error
            _write_log(log)  # the run log is written BEFORE the terminal event (P-5)
            yield {"type": "error", "step": "agent", "status": 502, "error": error, "terminated": "error"}
            return

        if terminated == "cap":
            reply = (f"I had to stop before finishing: this request reached its {cap_reason} limit. "
                     "Please try again with a simpler question.")
        log["steps"].append({"key": "agent", "status": "passed", "ms": agent_ms})
        yield {
            "type": "step", "key": "agent", "status": "passed", "kind": "in-process", "ms": agent_ms,
            "detail": f"{log['llm_calls']} model call(s), {len(log['tool_calls'])} tool call(s)"
                      + (f", stopped at {cap_reason} limit" if cap_reason else ""),
            "span": f"invoke_agent {getattr(agent, 'name', 'agent')}",
        }
        tokens = {"in": tokens_in, "out": tokens_out}

        # ---- Step 6: mask. From here on `reply` is the MASKED text; the unmasked one is dropped. ----
        yield {"type": "stage", "key": "mask", "label": "A2A Data Masker"}
        masked = await _do_mask(reply, user_email)
        if masked["status"] == "error":
            yield _fail(root, log, "mask", masked, t0, tokens)  # the customer gets NO reply, not an unmasked one
            return
        reply = masked["masked_text"]
        log["steps"].append({"key": "mask", "status": "passed", "ms": masked["ms"],
                             "detail": masked["detail"], "masked": masked["total"]})
        yield {"type": "step", "key": "mask", "status": "passed", "detail": masked["detail"],
               "ms": masked["ms"], "span": "security.a2a_mask", "kind": "A2A"}

        # ---- Step 7: save. Only the customer's message is stored, never the reply (R-4). ----
        yield {"type": "stage", "key": "save", "label": "Memory save (Mem0)"}
        saved = await _do_save(message, user_email)
        if saved["status"] == "error":
            yield _fail(root, log, "save", saved, t0, tokens)
            return
        log["steps"].append({"key": "save", "status": "passed", "ms": saved["ms"], "detail": saved["detail"]})
        yield {"type": "step", "key": "save", "status": "passed", "detail": saved["detail"],
               "ms": saved["ms"], "span": "memory.save", "kind": "Python fn"}

        wall = _ms(t0)  # the whole turn, including mask and save
        log.update(terminated=terminated, wall_clock_ms=wall, tokens=tokens)
        root.set_attribute("turn.terminated", terminated)
        root.set_attribute(SpanAttributes.OUTPUT_VALUE, reply)  # the masked reply: what the customer saw
        _write_log(log)  # the run log is written BEFORE the terminal event (P-5)
        yield {
            "type": "final", "blocked": False, "blocked_at": None, "response": reply,
            "terminated": terminated, "wall_clock_ms": wall, "tokens": tokens,
        }
