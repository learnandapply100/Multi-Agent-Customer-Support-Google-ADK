"""CLI front end. It runs NO pipeline logic: it only displays what support.pipeline yields (P-1).

Interactive:   python -m support.cli
Raw events:    echo "What is the status of order 3?" | SUPPORT_PASSWORD=alice \
                   python -m support.cli --user alice.jones@example.com --events
In --events mode, stdout carries NDJSON (one JSON event per line) and NOTHING else (C-3).
Messages are read from stdin, one per line. Everything else goes to stderr.
"""

import argparse
import asyncio
import getpass
import json
import os
import sys

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

load_dotenv()  # reads GOOGLE_API_KEY, DB passwords etc. from .env

from guards.judge_client import JUDGE_URL, judge_is_up
from guards.masker_client import MASKER_URL, masker_is_up
from support.agent import open_agent
from support.login import TOOLBOX_URL, check_login
from support.memory import mem0_is_configured
from support.pipeline import run_turn
from support.render import render
from support.telemetry import PHOENIX_URL, flush, phoenix_is_up, setup_tracing

APP_NAME = "customer_support"


def say(text: str) -> None:
    """Human-facing text. Always stderr so it can never pollute --events output."""
    print(text, file=sys.stderr, flush=True)


def next_message(events_mode: bool) -> str | None:
    """Return the next line, or None at end of input."""
    if events_mode:
        line = sys.stdin.readline()
        return line.strip() if line else None
    try:
        return input("You: ").strip()
    except EOFError:
        return None


def show(event: dict, events_mode: bool) -> None:
    if events_mode:
        print(json.dumps(event, default=str), flush=True)  # raw NDJSON, nothing else
    else:
        print(render(event), flush=True)  # one small block per event (Option B)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", help="log in as this email instead of being asked")
    parser.add_argument("--events", action="store_true", help="print raw events as NDJSON on stdout")
    args = parser.parse_args()

    if args.events and not args.user:
        say("--events needs --user (an email prompt would pollute stdout).")
        return 1

    # Refuse to start if Phoenix is down: otherwise turns would run with no trace (C-4, O-1).
    if not phoenix_is_up():
        say(f"Phoenix is not running at {PHOENIX_URL}. Start it first:  ./run.sh start")
        return 1
    # Same for the Judge: without it every turn would end in an error, so say so now (C-4).
    if not judge_is_up():
        say(f"The Security Judge is not running at {JUDGE_URL}. Start it first:  ./run.sh start")
        return 1
    # Same for the Masker: without it no reply could be checked for personal data (C-4).
    if not masker_is_up():
        say(f"The Data Masker is not running at {MASKER_URL}. Start it first:  ./run.sh start")
        return 1
    # Memory needs a Mem0 key. Without it every turn would end in an error at the recall step.
    if not mem0_is_configured():
        say("MEM0_API_KEY is missing. Add it to your .env file (memory, SPEC 5.7).")
        return 1
    setup_tracing()  # must happen BEFORE the agent is built, so its spans are captured

    email = args.user or input("Email: ").strip()
    password = os.getenv("SUPPORT_PASSWORD") if args.user else None
    if password is None:
        if args.events and not sys.stdin.isatty():
            say("--events with piped input needs the password in the SUPPORT_PASSWORD environment variable.")
            return 1
        password = getpass.getpass("Password: ")  # hidden input (C-1)

    try:
        user = await check_login(email, password)
    except Exception as exc:  # fail loud but readable: say which service is down (C-4)
        say(f"Cannot check the login: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}\n"
            f"Is the MCP Toolbox running at {TOOLBOX_URL}? Start it first (TECHNICAL.md stage 2).")
        return 1
    if user is None:
        say("Invalid email or password.")
        return 1
    if not args.events:
        say(f"Welcome, {user['full_name']}! Type quit to leave.")

    try:
        agent, toolbox = await open_agent(user["email"], user["full_name"])
    except Exception as exc:
        say(f"Cannot load the agent's tools: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}\n"
            f"Is the MCP Toolbox running at {TOOLBOX_URL}?")
        return 1
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id=user["email"])

    try:
        while (line := next_message(args.events)) is not None:
            if line.lower() in {"quit", "exit", "q"}:
                break
            if not line:
                continue
            async for event in run_turn(runner, user["email"], session.id, line):
                show(event, args.events)
    finally:
        await toolbox.close()
        flush()  # send queued spans to Phoenix before the program exits
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
