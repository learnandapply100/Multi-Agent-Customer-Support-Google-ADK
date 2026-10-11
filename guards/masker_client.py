"""The pipeline's side of the Masker: send the agent's reply over A2A, get the masked reply back (SPEC K-1).

There is NO fallback in this file. If the Masker is down, slow, or answers with anything we
cannot trust, we raise MaskerError and the pipeline ends the turn with an error (P-4). The
unmasked reply is NEVER returned "just in case": that would send personal data to the
customer while the step says everything is fine (section 11, R-4).
"""

import os
import uuid

import httpx

MASKER_URL = os.getenv("MASKER_URL", "http://127.0.0.1:10003")
MASKER_TIMEOUT_S = 2   # the Masker only runs patterns, so it answers in a few milliseconds


class MaskerError(Exception):
    """The Masker gave no usable result. The message says why, in words a person can read."""


def masker_is_up() -> bool:
    """True if the Masker serves its agent card. The CLI checks this before it starts (C-4)."""
    try:
        return httpx.get(f"{MASKER_URL}/.well-known/agent-card.json", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


async def ask_masker(text: str, user_email: str) -> dict:
    """Return {"masked_text", "counts", "total", "detail"}, or raise MaskerError."""
    request = {
        "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "data", "data": {"text": text, "user_email": user_email}}],
        }},
    }
    try:
        async with httpx.AsyncClient(timeout=MASKER_TIMEOUT_S) as client:
            reply = await client.post(MASKER_URL, json=request)
    except httpx.ConnectError as exc:
        raise MaskerError(f"Data Masker unreachable: cannot connect to {MASKER_URL}") from exc
    except httpx.TimeoutException as exc:
        raise MaskerError(f"Data Masker unreachable: no answer within {MASKER_TIMEOUT_S} s") from exc
    except httpx.HTTPError as exc:
        raise MaskerError(f"Data Masker unreachable: {type(exc).__name__}") from exc

    if reply.status_code != 200:
        raise MaskerError(f"Data Masker answered with HTTP {reply.status_code}")
    try:
        body = reply.json()
    except ValueError as exc:
        raise MaskerError("Data Masker answered with something that is not JSON") from exc
    if not isinstance(body, dict):
        raise MaskerError("Data Masker answered with something that is not a JSON-RPC reply")
    if body.get("error"):
        error = body["error"]
        raise MaskerError(f"Data Masker error: {error.get('message') if isinstance(error, dict) else error}")

    result = body.get("result")
    parts = result.get("parts") if isinstance(result, dict) else None
    data = next((p["data"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("data"), dict)), None)
    if data is None:
        raise MaskerError("Data Masker reply has no result in it")

    masked_text, total, detail = data.get("masked_text"), data.get("total"), data.get("detail")
    if not isinstance(masked_text, str):
        raise MaskerError("Data Masker reply has no masked text")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise MaskerError(f"Data Masker gave an unreadable count: {total!r}")
    if not isinstance(detail, str) or not detail:   # K-3: it must SAY what it did
        raise MaskerError("Data Masker did not say what it masked")

    # Two cheap honesty checks (K-2, R-8). The count and the text must tell the same story.
    if total == 0 and masked_text != text:
        raise MaskerError("Data Masker changed the reply but reported nothing masked")
    if total > 0 and masked_text == text:
        raise MaskerError(f"Data Masker reported {total} masked item(s) but returned the reply unchanged")

    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    return {"masked_text": masked_text, "counts": counts, "total": total, "detail": detail}
