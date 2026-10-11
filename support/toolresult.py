"""Small helpers for reading what the Toolbox and ADK hand back (SPEC section 7.1)."""

import json


def parse_rows(text) -> list[dict]:
    """Turn a Toolbox result string into a list of row dicts.

    The string can be empty (no rows), one object, a JSON array, or several objects
    stuck together, depending on the Toolbox version.
    """
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text or text == "null":
        return []
    decoder = json.JSONDecoder()
    rows, i = [], 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        value, i = decoder.raw_decode(text, i)
        rows.extend(value if isinstance(value, list) else [value])
    return rows


def read_tool_response(response) -> tuple[bool, object, str | None]:
    """Unwrap a tool response. Returns (ok, result, error).

    ADK hands us {"result": "<JSON string>"}: the real rows are inside that string.
    A failed call must come back as ok=False with a non-empty error (never silent).
    """
    if isinstance(response, dict) and response.get("error"):
        return False, None, str(response["error"])
    payload = response.get("result") if isinstance(response, dict) and "result" in response else response
    if isinstance(payload, str):
        try:
            return True, parse_rows(payload), None
        except ValueError:
            return False, None, f"unreadable tool result: {payload[:200]}"
    return True, payload, None


def decode_args(args: dict | None) -> dict:
    """The model sometimes sends a JSON object as a string (action-log's `parameters`). Decode it."""
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


def only_own_rows(rows, email: str):
    """Safety net (T-LEAK): drop any row owned by someone else. Returns (rows, number_dropped).

    The SQL already filters by the bound email, so this should never drop anything.
    If it ever does, we withhold the row instead of streaming another customer's data.
    """
    if not isinstance(rows, list):
        return rows, 0
    keep = [r for r in rows if not (isinstance(r, dict) and r.get("customer_email", email) != email)]
    return keep, len(rows) - len(keep)
