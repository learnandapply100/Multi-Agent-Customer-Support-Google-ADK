"""Measuring the traces in Phoenix (EVALS section 7; T-TRACE-ONE, T-TRACE-SHAPE).

The runner asks Phoenix for the spans of every turn it ran, and checks them by name:
  - exactly one root span, called agent.turn                      (T-TRACE-ONE)
  - a turn that ended `done` has every required span of SPEC 10   (T-TRACE-SHAPE)
  - a blocked turn has NO support-agent span: it stopped at the guard
  - an order answer has an execute_tool span for the expected tool (EVALS 3.5)
"""

import os
import time
from datetime import datetime

import httpx

from eval.client import EvalError

# Same defaults as support/telemetry.py (not imported, so the runner does not start tracing itself).
PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "customer-support")
EXPORT_WAIT_S = 60   # spans are sent in the background; give the last ones time to arrive

# SPEC section 10: a passing turn must contain all of these.
REQUIRED = ["agent.turn", "security.sanitize", "security.a2a_judge", "guardrail.check",
            "memory.recall", "invoke_agent", "security.a2a_mask", "memory.save"]


def _is_support_agent(name: str) -> bool:
    """The Guardrail is an agent too and has its own invoke_agent span. That one does not count."""
    return name.startswith("invoke_agent") and not name.endswith("guardrail_agent")


def fetch_spans(trace_ids: set[str], since: datetime) -> dict[str, list[dict]]:
    """Every span of the given traces, grouped by trace id. Reads page after page."""
    grouped: dict[str, list[dict]] = {trace_id: [] for trace_id in trace_ids}
    params = {"limit": 1000, "start_time": since.isoformat()}
    with httpx.Client(base_url=PHOENIX_URL, timeout=30) as client:
        while True:
            reply = client.get(f"/v1/projects/{PROJECT_NAME}/spans", params=params)
            if reply.status_code != 200:
                raise EvalError(f"Phoenix could not list spans: HTTP {reply.status_code} {reply.text[:120]}")
            body = reply.json()
            page = body.get("data") or []
            for span in page:
                trace_id = (span.get("context") or {}).get("trace_id")
                if trace_id in grouped:
                    grouped[trace_id].append(span)
            # Phoenix lists the newest spans first. Stop at the end, or once a whole page is
            # older than this run (in case this Phoenix version ignores start_time).
            all_older = bool(page) and all(datetime.fromisoformat(s["start_time"]) < since for s in page)
            if not body.get("next_cursor") or all_older:
                return grouped
            params["cursor"] = body["next_cursor"]


def wait_for_spans(trace_ids: set[str], since: datetime) -> dict[str, list[dict]]:
    """Fetch the spans, trying again until every trace has its root span or EXPORT_WAIT_S passed.

    The root span (agent.turn) ends last, so it is also the last one to arrive.
    """
    deadline = time.monotonic() + EXPORT_WAIT_S
    while True:
        grouped = fetch_spans(trace_ids, since)
        missing = [t for t, spans in grouped.items() if not any(s.get("name") == "agent.turn" for s in spans)]
        if not missing or time.monotonic() > deadline:
            return grouped
        time.sleep(5)


def check_trace(spans: list[dict], terminated: str) -> dict:
    """Return {"one_root": bool, "shape_ok": bool or None, "notes": [...]} for one turn."""
    names = [str(span.get("name")) for span in spans]
    roots = [str(span.get("name")) for span in spans if span.get("parent_id") is None]
    notes = []

    one_root = roots == ["agent.turn"]
    if not one_root:
        notes.append(f"trace: expected one root span 'agent.turn', found roots {roots or 'none'}")

    shape_ok = None   # only measured on turns that ended `done`
    if terminated == "done":
        missing = [need for need in REQUIRED
                   if not (any(_is_support_agent(n) for n in names) if need == "invoke_agent" else need in names)]
        shape_ok = not missing
        if missing:
            notes.append(f"trace: missing required span(s) {missing}")
    elif terminated == "blocked" and any(_is_support_agent(n) for n in names):
        notes.append("trace: this turn was blocked, but the support agent still has a span")
    return {"one_root": one_root, "shape_ok": shape_ok, "notes": notes}


def has_tool_span(spans: list[dict], tools: list[str]) -> bool:
    """True if the trace has an execute_tool span for one of these tools."""
    return any(span.get("name") == f"execute_tool {tool}" for span in spans for tool in tools)
