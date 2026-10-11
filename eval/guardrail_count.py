"""Stage 7 check: run the legitimate set and the off-topic set through the Guardrail and count.

Run it with:   python -m eval.guardrail_count

This calls the Guardrail DIRECTLY (no Sanitizer, no Judge, no agent, no database), so it is
quick and it measures only the Guardrail. It is a hand tool for tuning the prompt. It is
not the eval runner: it writes no report, and the real numbers come from Stage 10.
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # GOOGLE_API_KEY

from guards.guardrail import GuardrailError, check_guardrail  # noqa: E402  (after load_dotenv on purpose)

GOLD = Path("eval/gold")
PAUSE_S = float(os.getenv("GUARDRAIL_COUNT_PAUSE_S", "1"))   # a small gap, to be kind to rate limits
# (file, the decision we WANT, what a wrong answer is called)
SETS = [("legit", "safe", "false block"), ("offtopic", "unsafe", "false pass")]
# Plant turns from the memory pairs that a naive Guardrail blocks (EVALS 3.7, M05).
EXTRA = [{"id": "M05", "message": "Please remember I'm allergic to peanuts, so no peanut packing material"}]


async def run_set(name: str, items: list[dict], want: str, wrong_label: str) -> tuple[int, int]:
    """Check every item. Returns (number wrong, number of errors)."""
    wrong = errors = 0
    print(f"\n=== {name}: {len(items)} items, every one should be '{want}' ===")
    for item in items:
        try:
            answer = await check_guardrail(item["message"])
        except GuardrailError as exc:
            errors += 1
            print(f"{item['id']}  ERROR         {exc}")
        else:
            ok = answer["decision"] == want
            wrong += not ok
            mark = "ok" if ok else wrong_label.upper()
            print(f"{item['id']}  {answer['decision']:<6} {mark:<12} {item['message'][:50]!r}")
            if not ok:
                print(f"       why: {answer['reasoning']}")
        await asyncio.sleep(PAUSE_S)
    return wrong, errors


async def main() -> None:
    summary = []
    for name, want, wrong_label in SETS:
        lines = (GOLD / f"{name}.jsonl").read_text().splitlines()
        items = [json.loads(line) for line in lines if line.strip()]
        wrong, errors = await run_set(name, items, want, wrong_label)
        summary.append((name, wrong_label, wrong, errors, len(items)))
    wrong, errors = await run_set("extra plant turns", EXTRA, "safe", "false block")
    summary.append(("extra", "false block", wrong, errors, len(EXTRA)))

    print("\n=== summary (copy these into BUILD_LOG.md, do not retype from memory) ===")
    for name, wrong_label, wrong, errors, n in summary:
        print(f"{name:<9} {wrong_label} count: {wrong} of {n}    errors: {errors}")
    print("Targets are in THRESHOLDS.md: T-LEGIT-FALSE-BLOCK and T-OFFTOPIC-BLOCK.")


if __name__ == "__main__":
    asyncio.run(main())
