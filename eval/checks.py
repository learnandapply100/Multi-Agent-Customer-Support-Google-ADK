"""Checks the eval runner makes on ONE turn's event stream (EVALS section 1, law 2).

Everything here is plain arithmetic on the events. No model is asked for an opinion, so the
same stream always gets the same verdict.

  check_event_order(events)  the six ordering rules of SPEC section 7.3 (T-EVENT-ORDER)
  outcome(events)            how the turn ended: done, blocked, cap or error
"""

# SPEC P-2: the steps always run in this order.
STEP_ORDER = ["sanitize", "judge", "guardrail", "recall", "agent", "mask", "save"]
AGENT_EVENTS = ("llm", "tool_call", "tool_result")  # only allowed while the agent step is running


def check_event_order(events: list[dict]) -> list[str]:
    """Return a list of broken rules, in plain words. An empty list means the stream is correct."""
    if not events:
        return ["the stream was empty"]
    problems = []
    last = events[-1]

    # Rule 1: `trace` is the first event.
    if events[0].get("type") != "trace":
        problems.append(f"rule 1: the first event is {events[0].get('type')!r}, not 'trace'")

    # Rule 5: exactly one terminal event (final or error), and it is the last event.
    terminal_at = [i for i, e in enumerate(events) if e.get("type") in ("final", "error")]
    if terminal_at != [len(events) - 1]:
        problems.append(f"rule 5: expected one terminal event at the end, found them at positions {terminal_at}")

    # Rule 2: stages come in P-2 order, and each stage gets its own step before the next stage.
    # Rule 4: a blocked step is followed at once by final {blocked: true, same blocked_at}.
    started = []        # stage keys, in the order they began
    open_stage = None   # the stage that has begun but has no step yet
    for i, event in enumerate(events):
        kind, key = event.get("type"), event.get("key")
        if kind == "stage":
            if open_stage is not None:
                problems.append(f"rule 2: stage {key!r} began before step {open_stage!r} finished")
            started.append(key)
            open_stage = key
        elif kind == "step":
            if key != open_stage:
                problems.append(f"rule 2: step {key!r} arrived while stage {open_stage!r} was open")
            open_stage = None
            if event.get("status") == "blocked":
                nxt = events[i + 1] if i + 1 < len(events) else {}
                if not (nxt.get("type") == "final" and nxt.get("blocked") is True and nxt.get("blocked_at") == key):
                    problems.append(f"rule 4: blocked step {key!r} is not followed at once by a matching blocked final")
        elif kind in AGENT_EVENTS and open_stage != "agent":
            problems.append(f"rule 2: a {kind} event arrived outside the agent step")
    if started != STEP_ORDER[:len(started)]:
        problems.append(f"rule 2: stages ran in the wrong order: {started}")
    if last.get("type") == "final" and last.get("blocked") and not (
            len(events) >= 2 and events[-2].get("type") == "step" and events[-2].get("status") == "blocked"):
        problems.append("rule 4: the final event says blocked, but no blocked step came right before it")

    # Rule 3: every tool_call has exactly one tool_result with the same id, after it.
    call_at = {e.get("id"): i for i, e in enumerate(events) if e.get("type") == "tool_call"}
    result_at = [(e.get("id"), i) for i, e in enumerate(events) if e.get("type") == "tool_result"]
    for call_id, position in call_at.items():
        matches = [i for result_id, i in result_at if result_id == call_id]
        if len(matches) != 1:
            problems.append(f"rule 3: tool_call {call_id} has {len(matches)} results, expected exactly 1")
        elif matches[0] < position:
            problems.append(f"rule 3: the result of tool_call {call_id} arrived before the call")
    for result_id, _ in result_at:
        if result_id not in call_at:
            problems.append(f"rule 3: tool_result {result_id} has no tool_call")

    # Rule 6: nothing is saved on a blocked turn or an error turn. We look for a FINISHED save
    # (a `step` event). A save that was started and then failed did not save anything.
    saved = any(e.get("type") == "step" and e.get("key") == "save" for e in events)
    if saved and (last.get("type") == "error" or last.get("blocked")):
        problems.append("rule 6: a save step finished on a turn that ended blocked or in error")

    return problems


def outcome(events: list[dict]) -> tuple[str, str | None]:
    """How the turn ended, read from its last event: (terminated, blocked_at)."""
    last = events[-1] if events else {}
    if last.get("type") == "error":
        return "error", None
    if last.get("type") == "final":
        return str(last.get("terminated")), last.get("blocked_at")
    return "no terminal event", None
