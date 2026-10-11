"""The Security Judge as its own A2A service (SPEC J-1).

Run it with:   python -m guards.judge.server        (port 10002)

It serves two things:
  GET  /.well-known/agent-card.json   the agent card: who this agent is and what it does
  POST /                              JSON-RPC 2.0, A2A method "message/send"

The reply is an A2A message whose data part is {"verdict": "allow"|"block", "reason": "..."}.
"""

import os
import uuid

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

load_dotenv()  # the Judge is a separate process, so it reads GOOGLE_API_KEY from .env itself

from guards.judge.agent import MODEL, judge

HOST = os.getenv("JUDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("JUDGE_PORT", "10002"))

AGENT_CARD = {
    "name": "Security Judge",
    "description": "Decides whether one customer message is an attack. Returns an explicit allow or block verdict.",
    "url": f"http://{HOST}:{PORT}/",
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "preferredTransport": "JSONRPC",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["application/json"],
    "skills": [{
        "id": "judge_message",
        "name": "Judge a message",
        "description": "Checks a message with a pattern tool and, if that finds nothing, with a model "
                       f"({MODEL}). Output: {{\"verdict\": \"allow\"|\"block\", \"reason\": \"...\"}}.",
        "tags": ["security", "guard"],
    }],
}


def _rpc_error(request_id, code: int, message: str) -> JSONResponse:
    """A JSON-RPC error reply. The pipeline treats ANY of these as a failed Judge (P-4)."""
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
    texts = [p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not texts:
        return _rpc_error(request_id, -32602, "Invalid params: params.message.parts needs a text part")

    try:
        result = await judge("\n".join(texts))
    except Exception as exc:  # report the failure; NEVER turn it into "allow" (R-4)
        why = str(exc).splitlines()[0] if str(exc) else "no answer in time"
        return _rpc_error(request_id, -32603, f"Judge failed: {type(exc).__name__}: {why}")

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
