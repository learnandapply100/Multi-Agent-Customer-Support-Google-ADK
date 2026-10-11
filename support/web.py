"""Web front end. It runs NO pipeline logic: it only passes on what support.pipeline yields (P-1).

Start it:   python -m support.web          (port 8000, or set WEB_PORT)
Try it:     see the curl commands in TECHNICAL.md, Stage 9

The routes are the ones in SPEC section 7.2:
  POST /api/login    check the password, then build this user's agent with their email bound
  POST /api/chat     run one turn and stream its events, one JSON object per line (W-2)
  POST /api/logout   close this user's tools
  GET  /health       say which services are up
  GET  /             the HTML page

The CLI keeps ONE user in local variables. A web server can have many users at once, so
here each logged-in user gets an entry in the SESSIONS dictionary. That is the only real
difference from support/cli.py.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from toolbox_core import ToolboxClient

load_dotenv()  # reads GOOGLE_API_KEY, DB passwords etc. from .env

from guards.judge_client import JUDGE_URL, judge_is_up  # noqa: E402  (after load_dotenv on purpose)
from guards.masker_client import MASKER_URL, masker_is_up  # noqa: E402
from guards.sanitizer import MAX_CHARS  # noqa: E402  (the Sanitizer's length limit, reused for 413)
from support.agent import MODEL, open_agent  # noqa: E402
from support.login import TOOLBOX_URL, check_login  # noqa: E402
from support.memory import mem0_is_configured, recall  # noqa: E402
from support.pipeline import run_turn  # noqa: E402
from support.telemetry import PHOENIX_URL, flush, phoenix_is_up, setup_tracing  # noqa: E402
from support.toolresult import parse_rows  # noqa: E402

APP_NAME = "customer_support"
INDEX_HTML = Path(__file__).parent / "static" / "index.html"
# An address nobody owns. /health uses it to run real, harmless lookups.
HEALTH_USER = "health-check@example.invalid"


@dataclass
class UserSession:
    """Everything one logged-in user needs. The CLI holds the same things in local variables."""
    full_name: str
    runner: Runner
    session_id: str
    toolbox: ToolboxClient
    # One turn at a time per user. Two turns in the same ADK session at once would mix
    # their events together, so a second message waits for the first to finish.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


SESSIONS: dict[str, UserSession] = {}  # key: the user's email


def _why(exc: Exception) -> str:
    """One readable line from an exception."""
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_body(request: Request) -> dict | None:
    """The request body as a dict, or None if it is not a JSON object."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _close(session: UserSession) -> None:
    """Close a user's tools. Waits for a running turn to finish first, so we never cut one off."""
    async with session.lock:
        await session.toolbox.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once when the server starts (before `yield`) and once when it stops (after)."""
    # Same checks as the CLI: refuse to start, naming the missing service (C-4).
    if not phoenix_is_up():
        raise RuntimeError(f"Phoenix is not running at {PHOENIX_URL}. Start it first:  ./run.sh start")
    if not judge_is_up():
        raise RuntimeError(f"The Security Judge is not running at {JUDGE_URL}. Start it first:  ./run.sh start")
    if not masker_is_up():
        raise RuntimeError(f"The Data Masker is not running at {MASKER_URL}. Start it first:  ./run.sh start")
    if not mem0_is_configured():
        raise RuntimeError("MEM0_API_KEY is missing. Add it to your .env file (memory, SPEC 5.7).")
    setup_tracing()  # must happen BEFORE any agent is built, so its spans are captured
    yield
    for session in list(SESSIONS.values()):
        await _close(session)
    SESSIONS.clear()
    flush()  # send queued spans to Phoenix before the server exits


app = FastAPI(title="Customer Support", lifespan=lifespan)


@app.post("/api/login")
async def login(request: Request):
    body = await _read_body(request)
    if body is None:
        return _error(400, "the request body must be a JSON object")
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")

    try:
        user = await check_login(email, password)
    except Exception as exc:  # the Toolbox or the database is down: that is NOT a wrong password
        return _error(502, f"Cannot check the login: {_why(exc)}. Is the MCP Toolbox running at {TOOLBOX_URL}?")
    if user is None:
        return _error(401, "Invalid email or password.")

    # From here on this is the same as the CLI: the user's own tools, with their email bound (M-5).
    try:
        agent, toolbox = await open_agent(user["email"], user["full_name"])
    except Exception as exc:
        return _error(502, f"Cannot load the agent's tools: {_why(exc)}. Is the MCP Toolbox running at {TOOLBOX_URL}?")
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    adk_session = await sessions.create_session(app_name=APP_NAME, user_id=user["email"])

    old = SESSIONS.pop(user["email"], None)  # logging in again starts a fresh conversation
    if old is not None:
        await _close(old)
    SESSIONS[user["email"]] = UserSession(user["full_name"], runner, adk_session.id, toolbox)
    return {"user_id": user["email"], "full_name": user["full_name"],
            "is_premium": bool(user["is_premium_customer"])}


@app.post("/api/chat")
async def chat(request: Request):
    body = await _read_body(request)
    if body is None:
        return _error(400, "the request body must be a JSON object")
    user_id = str(body.get("user_id") or "").strip()
    message = body.get("message")
    message = message.strip() if isinstance(message, str) else ""  # the CLI strips each line too

    # These three are decided BEFORE streaming starts, so they can still be real HTTP statuses.
    session = SESSIONS.get(user_id)
    if session is None:
        return _error(401, "Not logged in.")
    if not message:
        return _error(400, "message is empty")
    if len(message) > MAX_CHARS:  # the same test the Sanitizer makes, done early so it can be a 413
        return _error(413, "message too long")

    async def stream():
        # run_turn is the SAME generator the CLI uses. We only turn each event into one line.
        # The `agent.turn` span is opened inside run_turn, and this generator is the one the
        # response iterates, so the agent's spans stay children of it (TECHNICAL troubleshooting).
        async with session.lock:
            async for event in run_turn(session.runner, user_id, session.session_id, message):
                yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        # Ask browsers and proxies not to hold lines back, so each one arrives as it is made.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/logout")
async def logout(request: Request):
    body = await _read_body(request)
    session = SESSIONS.pop(str((body or {}).get("user_id") or "").strip(), None)
    if session is not None:
        await _close(session)
    return {"ok": True}


async def _toolbox_and_db() -> tuple[str, str]:
    """Check the Toolbox, then the database THROUGH the Toolbox (no SQL in Python, M-2).

    It runs find-customer-orders for an address nobody owns. Zero rows back means the Toolbox
    reached Postgres and the query ran.
    """
    try:
        async with ToolboxClient(TOOLBOX_URL) as client:
            tools = await client.load_toolset(
                "support_agent", bound_params={"customer_email": HEALTH_USER, "user_email": HEALTH_USER})
            try:
                tool = next(t for t in tools if t.__name__ == "find-customer-orders")
                parse_rows(await tool())
            except Exception as exc:
                return "ok", _why(exc)
    except Exception as exc:
        return _why(exc), "not checked: the Toolbox is down"
    return "ok", "ok"


async def _mem0() -> str:
    """A real search for a user with no memories. It proves the key works and Mem0 answers."""
    try:
        await recall("health check", HEALTH_USER)
    except Exception as exc:
        return _why(exc)
    return "ok"


async def _is_up(check, name: str, url: str) -> str:
    # The three *_is_up functions are ordinary (blocking) functions, so run them in a thread.
    return "ok" if await asyncio.to_thread(check) else f"{name} is not answering at {url}"


@app.get("/health")
async def health():
    (toolbox, db), judge, masker, mem0, phoenix = await asyncio.gather(
        _toolbox_and_db(),
        _is_up(judge_is_up, "the Security Judge", JUDGE_URL),
        _is_up(masker_is_up, "the Data Masker", MASKER_URL),
        _mem0(),
        _is_up(phoenix_is_up, "Phoenix", PHOENIX_URL),
    )
    parts = {"db": db, "toolbox": toolbox, "judge": judge, "masker": masker, "mem0": mem0, "phoenix": phoenix}
    all_ok = all(value == "ok" for value in parts.values())
    body = {"status": "ok" if all_ok else "error", "model": MODEL, **parts}
    return JSONResponse(body, status_code=200 if all_ok else 503)


@app.get("/")
async def index():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML, media_type="text/html")
    # Until the page is built (Stage 9, part 2), say so instead of showing a 404.
    return HTMLResponse("<p>The server is running. The page (support/static/index.html) is not built yet.</p>")


if __name__ == "__main__":
    import uvicorn

    # 127.0.0.1: only this machine can reach it. Everything in this project runs locally.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("WEB_PORT", "8000")))
