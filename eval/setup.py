"""Getting to a known starting state before an eval run (EVALS section 2, AGENTS "Evidence").

  archive_runs()      move old run logs aside, so runs/*.json is exactly this eval run
  reset_db()          drop, recreate and seed the shop database (db/seed.sql)
  snapshot_orders()   the whole customer_orders table as text, to compare before and after
  clear_mem0(emails)  delete these users' memories and wait until they are really gone

The database work is done by running `psql` on .sql FILES. There is no SQL text in Python.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()  # MEM0_API_KEY lives in .env

from eval.client import RUNS_DIR, EvalError  # noqa: E402
from support.memory import MEM0_BASE_URL  # noqa: E402

DB_NAME = os.getenv("EVAL_DB_NAME", "online_shop")
MEM0_CLEAR_TIMEOUT_S = 90


def archive_runs() -> int:
    """Move runs/*.json into runs/archive/<date-time>/. Returns how many files moved.

    Nothing is deleted. runs/failing/ is a folder, so it is not touched.
    """
    old_logs = sorted(RUNS_DIR.glob("*.json"))
    if old_logs:
        target = RUNS_DIR / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
        target.mkdir(parents=True)
        for path in old_logs:
            shutil.move(str(path), str(target / path.name))
    return len(old_logs)


def _psql(sql_file: str, *extra: str) -> str:
    """Run one .sql file with psql and return what it printed. Raises EvalError if it fails."""
    # -w: never stop to ask for a password (nobody is there to type it); fail instead.
    command = ["psql", "-d", DB_NAME, "-w", "-q", "-v", "ON_ERROR_STOP=1", *extra, "-f", sql_file]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise EvalError("psql was not found. Run the eval in the same terminal setup you use for psql.") from exc
    if done.returncode != 0:
        raise EvalError(f"psql failed on {sql_file}: {done.stderr.strip()[:300]}")
    return done.stdout


def reset_db() -> None:
    """Same command as by hand: psql -d online_shop -v ON_ERROR_STOP=1 -1 -f db/seed.sql"""
    _psql("db/seed.sql", "-1")   # -1: all or nothing, in one transaction


def snapshot_orders() -> str:
    return _psql("db/snapshot_orders.sql", "-A", "-t")   # -A -t: plain rows, no table drawing


def _mem0_count(client: httpx.Client, email: str) -> int:
    reply = client.post("/v3/memories/", json={"filters": {"user_id": email}})
    if reply.status_code != 200:
        raise EvalError(f"Mem0 could not list memories for {email}: HTTP {reply.status_code} {reply.text[:120]}")
    body = reply.json()
    if isinstance(body, dict) and isinstance(body.get("count"), int):
        return body["count"]
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        return len(body["results"])
    raise EvalError("Mem0's list answer has no count, so we cannot tell whether the memories are gone")


def clear_mem0(emails: list[str]) -> None:
    """Delete every memory of these users (EVALS 3.7), then wait until Mem0 shows none left.

    Mem0 deletes in the background. If we planted new memories before the delete finished,
    we could not know whether they would survive. So we wait and check, and fail loudly if
    the memories are still there after MEM0_CLEAR_TIMEOUT_S.
    """
    key = os.getenv("MEM0_API_KEY", "").strip()
    if not key:
        raise EvalError("MEM0_API_KEY is missing from .env")
    try:
        with httpx.Client(base_url=MEM0_BASE_URL.rstrip("/"), timeout=15,
                          headers={"Authorization": f"Token {key}"}) as client:
            _delete_and_wait(client, emails)
    except httpx.HTTPError as exc:   # a network problem talking to Mem0, not to our web server
        raise EvalError(f"Mem0 could not be reached while clearing memories: {type(exc).__name__}") from exc


def _delete_and_wait(client: httpx.Client, emails: list[str]) -> None:
    for email in emails:
        reply = client.delete("/v1/memories/", params={"user_id": email})
        if reply.status_code not in (200, 204):
            raise EvalError(f"Mem0 refused to delete memories for {email}: "
                            f"HTTP {reply.status_code} {reply.text[:120]}")
    deadline = time.monotonic() + MEM0_CLEAR_TIMEOUT_S
    while True:
        left = [email for email in emails if _mem0_count(client, email) > 0]
        if not left:
            return
        if time.monotonic() > deadline:
            raise EvalError(f"Mem0 still has memories for {left} after {MEM0_CLEAR_TIMEOUT_S} s")
        time.sleep(5)
