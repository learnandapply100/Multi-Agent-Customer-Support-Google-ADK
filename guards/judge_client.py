"""The pipeline's side of the Judge: send one message over A2A, get a verdict back (SPEC J-1, J-4).

There is NO fallback in this file. If the Judge is down, slow, or answers with anything that
is not a clear allow or block, we raise JudgeError and the pipeline ends the turn with an
error (P-4). A guard that cannot decide must never let the message through (section 11, R-4).
"""

import os
import uuid

import httpx

JUDGE_URL = os.getenv("JUDGE_URL", "http://127.0.0.1:10002")
JUDGE_TIMEOUT_S = 10   # a little more than the Judge's own 8 s model timeout, so its error reaches us


class JudgeError(Exception):
    """The Judge gave no usable verdict. The message says why, in words a person can read."""


def judge_is_up() -> bool:
    """True if the Judge serves its agent card. The CLI checks this before it starts (C-4)."""
    try:
        return httpx.get(f"{JUDGE_URL}/.well-known/agent-card.json", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


async def ask_judge(text: str) -> dict:
    """Return {"verdict": "allow"|"block", "reason", "source", "tokens"}, or raise JudgeError."""
    request = {
        "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
        }},
    }
    try:
        async with httpx.AsyncClient(timeout=JUDGE_TIMEOUT_S) as client:
            reply = await client.post(JUDGE_URL, json=request)
    except httpx.ConnectError as exc:
        raise JudgeError(f"Security Judge unreachable: cannot connect to {JUDGE_URL}") from exc
    except httpx.TimeoutException as exc:
        raise JudgeError(f"Security Judge unreachable: no answer within {JUDGE_TIMEOUT_S} s") from exc
    except httpx.HTTPError as exc:
        raise JudgeError(f"Security Judge unreachable: {type(exc).__name__}") from exc

    if reply.status_code != 200:
        raise JudgeError(f"Security Judge answered with HTTP {reply.status_code}")
    try:
        body = reply.json()
    except ValueError as exc:
        raise JudgeError("Security Judge answered with something that is not JSON") from exc
    if not isinstance(body, dict):
        raise JudgeError("Security Judge answered with something that is not a JSON-RPC reply")
    if body.get("error"):   # the Judge itself failed, e.g. its model was down or timed out
        error = body["error"]
        raise JudgeError(f"Security Judge error: {error.get('message') if isinstance(error, dict) else error}")

    result = body.get("result")
    parts = result.get("parts") if isinstance(result, dict) else None
    data = next((p["data"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("data"), dict)), None)
    if data is None:
        raise JudgeError("Security Judge reply has no verdict in it")
    if data.get("verdict") not in ("allow", "block"):   # J-3: explicit verdict or nothing
        raise JudgeError(f"Security Judge gave an unknown verdict: {data.get('verdict')!r}")

    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    return {
        "verdict": data["verdict"], "reason": str(data.get("reason") or "no reason given"),
        "source": str(data.get("source") or "unknown"),
        "tokens": {"in": int(tokens.get("in") or 0), "out": int(tokens.get("out") or 0)},
    }
