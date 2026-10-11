"""Gate 0 STATIC and the two red lines that can be checked without running a turn.

  gate_static()            the linter is clean; no .env, runs/ or reports/ file is in git
  secret_in_repo()         red line: a key, a password or the .env file is tracked by git
  mutating_tool_loaded()   red line: the agent's toolset has a tool that can change customer_orders

Nothing here prints a secret. It only says WHICH file one was found in.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

TOOLS_YAML = Path("mcp_toolbox/tools.yaml")
AGENT_TOOLSET = "support_agent"
PRIVATE_PATHS = (".env", "runs/", "reports/")
SECRET_NAMES = ("GOOGLE_API_KEY", "MEM0_API_KEY", "DB_PASSWORD", "LOGIN_DB_PASSWORD")
# SQL that would change customer_orders. The agent's tools must contain none of it (M-6).
CHANGES_ORDERS = re.compile(
    r"\b(update|delete\s+from|insert\s+into|truncate(\s+table)?|drop\s+table|alter\s+table)\s+customer_orders\b",
    re.IGNORECASE)


def _git(*args: str) -> list[str] | None:
    """Output lines of a git command, or None if this folder is not a git repository."""
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=30)
    except FileNotFoundError:
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def _is_private(path: str) -> bool:
    """True for a file that must stay out of git. SUBMISSION.md asks for exactly two exceptions."""
    if path == "reports/eval.json" or path.startswith("runs/failing/"):
        return False
    return path == ".env" or path.startswith(PRIVATE_PATHS[1:])


def secret_in_repo() -> tuple[bool, list[str]]:
    """Red line. True if .env is tracked, or a tracked file contains one of the real secret values."""
    tracked = _git("ls-files") or []
    found = [f"{path} is tracked by git" for path in tracked if path == ".env"]
    # Short values are skipped: a 5-letter password would match ordinary words by accident.
    secrets = {name: os.getenv(name, "") for name in SECRET_NAMES if len(os.getenv(name, "")) >= 8}
    for path in tracked:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > 1_000_000:
            continue
        text = file.read_text(errors="ignore")
        found += [f"{path} contains the value of {name}" for name, value in secrets.items() if value in text]
    return bool(found), found


def mutating_tool_loaded() -> tuple[bool, list[str]]:
    """Red line (T-MUTATE). Reads tools.yaml: does any tool in the agent's toolset change customer_orders?"""
    documents = [d for d in yaml.safe_load_all(TOOLS_YAML.read_text()) if isinstance(d, dict)]
    statements = {d["name"]: str(d.get("statement", "")) for d in documents if d.get("kind") == "tool"}
    toolset = next((d for d in documents if d.get("kind") == "toolset" and d.get("name") == AGENT_TOOLSET), {})
    found = [f"tool {name} can change customer_orders" for name in toolset.get("tools") or []
             if CHANGES_ORDERS.search(statements.get(name, ""))]
    return bool(found), found


def gate_static() -> tuple[list[str], list[str]]:
    """Gate 0. Returns (failures, warnings). Any failure blocks the run."""
    failures, warnings = [], []

    staged, tracked = _git("diff", "--cached", "--name-only"), _git("ls-files")
    if staged is None or tracked is None:
        warnings.append("this folder is not a git repository, so the git checks could not run")
    else:
        failures += [f"{path} is staged for commit" for path in staged if _is_private(path)]
        failures += [f"{path} is tracked by git" for path in tracked if _is_private(path)]
    leaked, where = secret_in_repo()
    failures += [w for w in where if w not in failures] if leaked else []

    if shutil.which("ruff"):
        done = subprocess.run(["ruff", "check", "."], capture_output=True, text=True, check=False, timeout=120)
        if done.returncode != 0:
            lines = (done.stdout or done.stderr).strip().splitlines()
            failures.append("the linter (ruff check .) reported problems:\n        " + "\n        ".join(lines[:15]))
    else:
        warnings.append("no linter was run: `ruff` is not installed (pip install ruff), "
                        "and no type checker is wired in. Gate 0 is only partly checked.")
    return failures, warnings
