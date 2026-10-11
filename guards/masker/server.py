"""The Data Masker as its own A2A service (SPEC K-1).

Run it with:   python -m guards.masker.server        (port 10003)

It serves two things, the same way the Judge does:
  GET  /.well-known/agent-card.json   the agent card: who this agent is and what it does
  POST /                              JSON-RPC 2.0, A2A method "message/send"

The request carries one data part:  {"text": "<the agent's reply>", "user_email": "<logged-in user>"}
The reply is an A2A message whose data part is
  {"masked_text": "...", "counts": {"email": 0, "phone": 1, "card": 0}, "total": 1, "detail": "masked 1 phone number"}

There is NO model in this service (Stage 8 decision). The work is done by mask_text() in
masking.py, which is plain pattern matching, so it can never rewrite the reply (K-2, R-8).
"""

import os
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from guards.masker.masking import mask_text

HOST = os.getenv("MASKER_HOST", "127.0.0.1")
PORT = int(os.getenv("MASKER_PORT", "10003"))

AGENT_CARD = {
    "name": "Data Masker",
    "description": "Hides personal data in a support reply: email addresses other than the customer's own, "
                   "phone numbers and card-like numbers. It changes nothing else.",
    "url": f"http://{HOST}:{PORT}/",
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "preferredTransport": "JSONRPC",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [{
        "id": "mask_pii",
        "name": "Mask personal data",
        "description": "Input: a data part {\"text\": \"...\", \"user_email\": \"...\"}. Pattern matching only, no model. "
                       "Output: {\"masked_text\": \"...\", \"counts\": {...}, \"total\": 0, \"detail\": \"...\"}.",
        "tags": ["privacy", "pii", "guard"],
    }],
}


def _rpc_error(request_id, code: int, message: str) -> JSONResponse:
    """A JSON-RPC error reply. The pipeline treats ANY of these as a failed Masker (P-4)."""
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


async def agent_card(_: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


async def rpc(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _rpc_error(None, -32700, "Parse error: the request body is not JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid request: not a JSON-RPC 2.0 object")
    request_id = body.get("id")
    if body.get("method") != "message/send":
        return _rpc_error(request_id, -32601, f"Method not found: {body.get('method')!r}. This agent supports message/send.")

    params = body.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    parts = message.get("parts") if isinstance(message, dict) else None
    data = next((p["data"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("data"), dict)), None)
    if data is None:
        return _rpc_error(request_id, -32602, "Invalid params: params.message.parts needs a data part")
    text, user_email = data.get("text"), data.get("user_email")
    if not isinstance(text, str):
        return _rpc_error(request_id, -32602, "Invalid params: data.text must be a string")
    if not isinstance(user_email, str) or not user_email.strip():
        # Without the user's email we could not tell their own address from someone else's.
        return _rpc_error(request_id, -32602, "Invalid params: data.user_email must be a non-empty string")

    try:
        result = mask_text(text, user_email)
    except Exception as exc:  # report the failure; NEVER answer with the unmasked text (R-4)
        why = str(exc).splitlines()[0] if str(exc) else "no detail"
        return _rpc_error(request_id, -32603, f"Masker failed: {type(exc).__name__}: {why}")

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {
        "kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
        "parts": [{"kind": "data", "data": result}],
    }})


app = Starlette(routes=[
    Route("/.well-known/agent-card.json", agent_card),   # current A2A path
    Route("/.well-known/agent.json", agent_card),        # older path, same card
    Route("/", rpc, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
