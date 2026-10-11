"""Turns one pipeline event into a small readable block of text (Option B).

Pure display: no logic about the turn lives here (P-1). The same function will draw the
guard steps, the recall list and blocked turns when those stages exist.
"""

import json

PAD = 10  # width of the label column: "    time:    4199 ms"
SKIP_KEYS = {"customer_email", "delivery_address"}  # long, and the customer already knows them


def _line(label: str, value) -> str:
    return f"    {label + ':':<{PAD}}{value}"


def _more(value) -> str:
    """A continuation line that lines up under the first value."""
    return f"    {'':<{PAD}}{value}"


def _tokens(t_in, t_out) -> str:
    return f"{t_in} in / {t_out} out"


def _row_summary(row) -> str:
    if not isinstance(row, dict):
        return str(row)
    parts = [f"{k}={v}" for k, v in row.items() if not isinstance(v, (list, dict)) and k not in SKIP_KEYS]
    items = row.get("items")
    if isinstance(items, list):
        names = [str(i.get("product")) for i in items if isinstance(i, dict) and i.get("product")]
        if names:
            parts.append("items=" + ", ".join(names))
    text = " ".join(parts)
    return text if len(text) <= 110 else text[:107] + "..."


def _args(args: dict) -> str:
    if not args:
        return "(none)"
    return ", ".join(f"{k} = {json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in args.items())


def render(event: dict) -> str:
    kind = event.get("type")
    out: list[str]

    if kind == "trace":
        out = [f"[trace] {event['turn_id']}",
               _line("trace id", event["trace_id"]),
               _line("url", event.get("url") or "(not available yet: Phoenix arrives in Stage 5)")]

    elif kind == "stage":
        out = [f"[{event['key']}] started: {event.get('label', '')}"]

    elif kind == "llm":
        out = [f"[model call] {event['decision']}",
               _line("model", event["model"]),
               _line("tokens", _tokens(event["tokens_in"], event["tokens_out"])),
               _line("time", f"{event['ms']} ms"),
               _line("span", event["span"])]

    elif kind == "tool_call":
        info = event.get("info", {})
        out = [f"[tool call #{event['id']}] {event['name']}",
               _line("args", _args(event.get("args", {}))),
               _line("access", f"{info.get('kind', '?')}, {info.get('access', '?')}"),
               _line("span", event["span"])]

    elif kind == "tool_result":
        status = "ok" if event["ok"] else "FAILED"
        out = [f"[tool result #{event['id']}] {event['name']}  {status}"]
        if event["ok"]:
            rows = event.get("result")
            if isinstance(rows, list):
                out.append(_line("rows", f"{len(rows)}" if rows else "0 (nothing found)"))
                out += [_more(_row_summary(r)) for r in rows[:5]]
                if len(rows) > 5:
                    out.append(_more(f"... and {len(rows) - 5} more"))
            else:
                out.append(_line("result", rows))
        else:
            out.append(_line("error", event.get("error", "")))
        if event.get("withheld_rows"):
            out.append(_line("WITHHELD", f"{event['withheld_rows']} row(s) owned by someone else were removed"))
        out.append(_line("time", f"{event['ms']} ms"))

    elif kind == "step":
        label = "BLOCKED" if event["status"] == "blocked" else event["status"]
        out = [f"[{event['key']}] {label}"]
        if event.get("detail"):
            out.append(_line("detail", event["detail"]))
        for m in event.get("memories", []):  # the recall step lists every candidate (R-5)
            tag = "inserted" if m.get("inserted") else "skipped"
            why = "" if m.get("inserted") else f", {m.get('reason')}"
            out.append(_line(tag, f"\"{m['memory']}\" (score {m['score']}{why})"))
        out.append(_line("time", f"{event['ms']} ms"))
        out.append(_line("span", event["span"]))
        if event["status"] == "blocked":
            out.append(_line("note", "turn ended here: nothing after this step ran"))

    elif kind == "final":
        head = f"BLOCKED at {event['blocked_at']}" if event.get("blocked") else event["terminated"]
        reply = (event.get("response") or "").splitlines() or [""]
        out = [f"[final] {head}", _line("reply", reply[0])] + [_more(r) for r in reply[1:]]
        out += [_line("time", f"{event['wall_clock_ms']} ms"),
                _line("tokens", _tokens(event["tokens"]["in"], event["tokens"]["out"]))]

    elif kind == "error":
        out = [f"[ERROR] {event['step']}  (status {event.get('status', '?')})",
               _line("what", event["error"]),
               _line("note", "turn ended with an error: no answer was given")]

    else:
        out = [f"[{kind}] {json.dumps(event, default=str)}"]

    return "\n".join(out) + "\n"
