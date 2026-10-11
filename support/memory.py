"""Memory: what the customer told us before, kept in Mem0 (SPEC R-1 to R-6).

Two functions the PIPELINE calls on every turn. The agent has no memory tool (R-6), so the
model can never decide to skip them (section 11, R-2).

  recall(message, user_email)   BEFORE the agent: search this user's memories (R-1, R-2, R-5)
  save(message, user_email)     AFTER the Masker: store the user's message, never the reply (R-4)

We call Mem0's REST API directly with httpx (already used for the Judge and Masker), so there
is no extra package to install and no SDK version to match.

There is NO fallback in this file. If Mem0 is down or answers with something we cannot read,
we raise Mem0Error and the pipeline ends the turn with an error. A turn that silently ran
without memory would look exactly like a turn where the customer had no memories.
"""

import os

import httpx

MEM0_BASE_URL = os.getenv("MEM0_BASE_URL", "https://api.mem0.ai")
SEARCH_PATH = "/v3/memories/search/"
ADD_PATH = "/v3/memories/add/"

# The numbers are defined in THRESHOLDS.md section 4. If they change there, change them here.
TOP_K = 5          # T-MEM-TOPK:     memories requested per recall
# T-MEM-MINSCORE: a memory below this score is not inserted.
# THRESHOLDS.md declares 0.25, measured on the reference build. Stage 8 decision: changed to
# 0.16 for THIS build, with evidence (see DESIGN.md, Trade-offs). On five memory pairs
# (M01 to M05) the right memory scored 0.1731 to 0.3317 on its first ask, and the highest
# score of an unrelated memory was 0.1507. At 0.25 only 1 of the 5 was inserted.
MIN_SCORE = 0.16
MAX_CHARS = 500    # T-MEM-MAXCHARS: a memory longer than this is not inserted (R-7)

RECALL_TIMEOUT_S = 5
SAVE_TIMEOUT_S = 4   # the pipeline keeps a few seconds of the turn's time budget for mask + save
PREVIEW_CHARS = 120  # how much of an over-long memory we still show in the event and the trace

# R-3: inserted memories go at the top of the agent's input under this fixed header.
HEADER = "Relevant memories about this customer (from Mem0):"


class Mem0Error(Exception):
    """Mem0 gave no usable answer. The message says why, in words a person can read."""


def mem0_is_configured() -> bool:
    """True if the API key is set. The CLI checks this before it starts."""
    return bool(os.getenv("MEM0_API_KEY", "").strip())


async def _post(path: str, body: dict, timeout_s: float):
    """One POST to Mem0. Returns the decoded JSON, or raises Mem0Error."""
    key = os.getenv("MEM0_API_KEY", "").strip()
    if not key:
        raise Mem0Error("Mem0 is not configured: MEM0_API_KEY is missing from .env")
    url = MEM0_BASE_URL.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            reply = await client.post(url, json=body, headers={"Authorization": f"Token {key}"})
    except httpx.ConnectError as exc:
        raise Mem0Error(f"Mem0 unreachable: cannot connect to {MEM0_BASE_URL}") from exc
    except httpx.TimeoutException as exc:
        raise Mem0Error(f"Mem0 unreachable: no answer within {timeout_s} s") from exc
    except httpx.HTTPError as exc:
        raise Mem0Error(f"Mem0 unreachable: {type(exc).__name__}") from exc

    if reply.status_code in (401, 403):
        raise Mem0Error(f"Mem0 rejected the API key (HTTP {reply.status_code})")
    if reply.status_code != 200:
        raise Mem0Error(f"Mem0 answered with HTTP {reply.status_code}: {reply.text[:120]}")
    try:
        return reply.json()
    except ValueError as exc:
        raise Mem0Error("Mem0 answered with something that is not JSON") from exc


async def recall(message: str, user_email: str) -> list[dict]:
    """Search this user's memories with their message (R-1) and decide which may be inserted (R-2).

    Returns EVERY candidate (R-5), in Mem0's order, each as:
        {"memory": str, "score": float, "inserted": bool, "reason": None | "below cutoff ..." | "too long ..."}
    """
    body = await _post(SEARCH_PATH, {
        "query": message,
        "filters": {"user_id": user_email},   # only this customer's memories
        "top_k": TOP_K,
        # Mem0 hides low scores by default. We ask for all of them, so that OUR cutoff decides
        # and every skipped memory is still reported with its reason.
        "threshold": 0.0,
    }, RECALL_TIMEOUT_S)

    rows = body.get("results") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise Mem0Error("Mem0 search answer has no results list")

    candidates = []
    for row in rows[:TOP_K]:
        if not isinstance(row, dict) or not isinstance(row.get("memory"), str):
            raise Mem0Error("Mem0 search answer has a result with no memory text")
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise Mem0Error("Mem0 search answer has a result with no score")
        # Safety net, like only_own_rows for tools: the filter above already limits the search
        # to this user. If a row ever names someone else, fail loudly and show nothing.
        if row.get("user_id") not in (None, user_email):
            raise Mem0Error("Mem0 returned a memory that belongs to another user")

        text, score = row["memory"], round(float(score), 4)
        if len(text) > MAX_CHARS:
            candidates.append({"memory": text[:PREVIEW_CHARS] + "...", "score": score, "inserted": False,
                               "reason": f"too long ({len(text)} > {MAX_CHARS} chars)"})
        elif score < MIN_SCORE:
            candidates.append({"memory": text, "score": score, "inserted": False,
                               "reason": f"below cutoff ({score} < {MIN_SCORE})"})
        else:
            candidates.append({"memory": text, "score": score, "inserted": True, "reason": None})
    return candidates


def build_agent_input(message: str, candidates: list[dict]) -> str:
    """Put the inserted memories above the customer's message, under the fixed header (R-3)."""
    inserted = [c["memory"] for c in candidates if c["inserted"]]
    if not inserted:
        return message   # nothing to add: the agent sees the message exactly as typed
    lines = [HEADER] + [f"- {memory}" for memory in inserted]
    return "\n".join(lines) + "\n\nCustomer message:\n" + message


async def save(message: str, user_email: str) -> dict:
    """Store what the USER said (R-4, section 11 R-5). The agent's reply is never passed in here.

    Mem0 extracts facts in the background, so a good answer is usually status PENDING: the
    memory can take a couple of minutes to show up in recall (T-MEM-WAIT).
    """
    body = await _post(ADD_PATH, {
        "messages": [{"role": "user", "content": message}],
        "user_id": user_email,
    }, SAVE_TIMEOUT_S)

    status = body.get("status") if isinstance(body, dict) else None
    if status == "FAILED":
        raise Mem0Error("Mem0 reported that the save FAILED")
    return {"status": str(status or "accepted"), "event_id": body.get("event_id") if isinstance(body, dict) else None}
