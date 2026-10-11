"""How the eval runner talks to the product: through the web server, like a browser would.

EVALS section 3: every gold item is ONE turn: log in as the user, send the message, read the
event stream to its last event. We log in again before every item, because web.py starts a
fresh conversation at each login. That way one item can never colour the next one.

After the turn, the run log that the PIPELINE wrote is read from runs/<turn_id>.json
(EVALS 4.1). The runner never rebuilds that log from the stream.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.getenv("EVAL_BASE_URL", "http://localhost:8000")
RUNS_DIR = Path("runs")
GOLD_DIR = Path(__file__).parent / "gold"
TURN_TIMEOUT_S = 60  # T-BUD-WALL is 30 s; twice that means the server is stuck, not slow

# The gold files name users by first name. Every password is that first name (EVALS section 2).
USERS = {
    "alice": "alice.jones@example.com", "bob": "bob.smith@techmail.com",
    "charlie": "charlie.d@webmail.com", "diana": "diana.prince@hero.net",
    "evan": "evan.g@bizcorp.com", "fiona": "fiona.shrek@swamp.com",
    "george": "george.j@jungle.com", "hannah": "hannah.m@school.edu",
    "ian": "ian.malcolm@chaos.com", "julia": "julia.child@kitchen.com",
}


class EvalError(Exception):
    """The runner itself could not do its job (server down, login refused, log missing)."""


def load_set(name: str) -> list[dict]:
    """Read eval/gold/<name>.jsonl: one JSON object per line."""
    path = GOLD_DIR / f"{name}.jsonl"
    if not path.exists():
        raise EvalError(f"gold set not found: {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def open_client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=TURN_TIMEOUT_S)


def run_item(client: httpx.Client, user: str, message: str) -> dict:
    """Run one turn as `user` (a first name). Returns {"events", "log", "seconds", "email"}."""
    email = USERS[user]
    login = client.post("/api/login", json={"email": email, "password": user})
    if login.status_code != 200:
        raise EvalError(f"cannot log in as {user}: HTTP {login.status_code} {login.text[:120]}")

    events = []
    start = time.monotonic()
    with client.stream("POST", "/api/chat", json={"user_id": email, "message": message}) as reply:
        if reply.status_code != 200:   # 400, 401 or 413: refused before any event was made
            reply.read()
            raise EvalError(f"chat refused for {user}: HTTP {reply.status_code} {reply.text[:120]}")
        for line in reply.iter_lines():   # one event per line, as it arrives
            if line.strip():
                events.append(json.loads(line))
    seconds = time.monotonic() - start

    # The first event names the turn. Its run log must already be on disk (P-5).
    return {"events": events, "log": _read_log(events), "seconds": seconds, "email": email}


def _read_log(events: list[dict]) -> dict:
    turn_id = events[0].get("turn_id") if events and events[0].get("type") == "trace" else None
    log_path = RUNS_DIR / f"{turn_id}.json"
    if turn_id is None or not log_path.exists():
        raise EvalError(f"no run log for this turn (looked for {log_path}). "
                        "Run the eval from the project folder, on the machine the product runs on.")
    return json.loads(log_path.read_text())


def run_cli_item(user: str, message: str) -> dict:
    """Run one turn through the CLI's --events mode instead of the web server.

    G-EVENTS needs the event rules to hold for BOTH front ends, so a few turns go this way.
    """
    email = USERS[user]
    start = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, "-m", "support.cli", "--user", email, "--events"],
            input=message + "\n", capture_output=True, text=True, check=False, timeout=120,
            env={**os.environ, "SUPPORT_PASSWORD": user},
        )
    except subprocess.TimeoutExpired as exc:
        raise EvalError("the CLI did not finish within 120 s") from exc
    if done.returncode != 0:
        raise EvalError(f"the CLI exited with code {done.returncode}: {done.stderr.strip()[-200:]}")
    try:
        events = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    except ValueError as exc:   # C-3: stdout must carry JSON events and nothing else
        raise EvalError("the CLI printed something that is not JSON on stdout in --events mode") from exc
    if not events:
        raise EvalError("the CLI printed no events")
    return {"events": events, "log": _read_log(events), "seconds": time.monotonic() - start, "email": email}
