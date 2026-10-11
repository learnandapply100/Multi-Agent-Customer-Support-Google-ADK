"""The eval runner (EVALS section 5). Run it from the project folder, with every service and
the web server up:

    python -m eval.run

What it does, in order:
  Gate 0 STATIC      the linter is clean; no .env, runs/ or reports/ file is in git.
  Gate 1 HEALTH      GET /health says every dependency is live.
  (setup)            old run logs are moved to runs/archive/, the database is reset.
  Gate 2 SMOKE       O01 ends done, X01 is blocked at the judge, F01 at the guardrail.
  The seven sets     memory PLANTS first, then attack, off-topic, probe and legitimate, then a
                     database reset, then order and action, then the memory ASKS. The other
                     sets fill the T-MEM-WAIT time between plant and ask (Stage 10 decision).
  Gate 3 TRAJECTORY  over runs/*.json: failed tools have errors, nothing failed silently, budgets held.
  Gate 4 EVAL        every T-* row of THRESHOLDS, written to reports/eval.json.
  Gate 5 HUMAN       yours: the runner names two turns for you to read end to end in Phoenix.

Gates 0 to 2 stop the run when they fail (EVALS law 4). Gates 3 and 4 are both worked out
and written to the report even if one fails, because the report is the evidence.
Exit code: 0 = every gate passed, 1 = warnings only, 2 = at least one gate failed.
The full run takes about 20 minutes.

A QUICK run, for checking one change without waiting 20 minutes:

    python -m eval.run --only order            (one set)
    python -m eval.run --only probe,action     (several sets)

Set names: attack, offtopic, probe, legit, order, action, memory. A quick run checks /health,
resets the database, runs only those sets and prints their scores and latency. It does NOT
write reports/eval.json and does NOT move old run logs: only a full run is evidence.
"""

import argparse
import os
import sys
import time
from datetime import UTC, datetime

import httpx

from eval.checks import check_event_order, outcome
from eval.client import (
    BASE_URL,
    USERS,
    EvalError,
    load_set,
    open_client,
    run_cli_item,
    run_item,
)
from eval.report import (
    REPORT_PATH,
    add_trace_checks,
    build_metrics,
    gate_trajectory,
    percentile,
    write_report,
)
from eval.scoring import (
    facts,
    score_action,
    score_ask,
    score_legit,
    score_must_block,
    score_order,
    score_plant,
    score_probe,
    unknown_tools,
)
from eval.setup import archive_runs, clear_mem0, reset_db, snapshot_orders
from eval.static import gate_static
from eval.traces import wait_for_spans

# T-MEM-WAIT (THRESHOLDS section 4) is a MINIMUM of 120 s. You may wait longer, never less.
MEM_WAIT_S = max(120, int(os.getenv("EVAL_MEM_WAIT_S", "120")))
DELAY_S = float(os.getenv("EVAL_DELAY_S", "1"))  # a short pause between turns, kind to the rate limit
SPAN_SETTLE_S = 10  # spans are exported in the background; wait before reading them (EVALS 7)

# Gate 2: (gold set file, item id, how the turn must end, where it must be blocked).
SMOKE = [
    ("order", "O01", "done", None),
    ("attack", "X01", "blocked", "judge"),
    ("offtopic", "F01", "blocked", "guardrail"),
]


def run_one(client, set_name: str, item: dict, message: str, score, via_cli: bool = False) -> dict:
    """Run one gold item, score it, print one line, and return its row for the report."""
    row = {"set": set_name, "id": item["id"], "user": item["user"], "pass": False, "notes": [],
           "started_at": time.monotonic()}
    try:
        result = run_cli_item(item["user"], message) if via_cli else run_item(client, item["user"], message)
    except (EvalError, httpx.HTTPError) as exc:
        # The runner could not complete this turn. Record it loudly and carry on with the rest.
        row.update(terminated="runner_error", notes=[f"{type(exc).__name__}: {exc}"])
        print(f"  {item['id']}  FAIL  the runner could not complete this turn: {row['notes'][0]}")
        return row

    events, log = result["events"], result["log"]
    terminated, blocked_at = outcome(events)
    row.update(score(item, result))
    row.update(facts(events))
    row.update(
        trace_id=log.get("trace_id"), turn_id=log.get("turn_id"), terminated=terminated, blocked_at=blocked_at,
        ms=log.get("wall_clock_ms"), event_problems=check_event_order(events),
        unknown_tools=unknown_tools(result), finished_at=time.monotonic(),
    )
    # The run log and the stream must tell the same story about how the turn ended.
    if log.get("terminated") != terminated:
        row["event_problems"].append(f"the run log says {log.get('terminated')!r} but the stream says {terminated!r}")
    if terminated == "error":
        row["notes"].append(f"the turn ended in an error: {events[-1].get('error')}")

    where = f" at {blocked_at}" if blocked_at else ""
    print(f"  {item['id']}  {'pass' if row['pass'] else 'FAIL'}  {terminated}{where}  {row['ms']} ms")
    for line in row["notes"] + row["event_problems"] + [f"unknown tool called: {t}" for t in row["unknown_tools"]]:
        print(f"        {line}")
    time.sleep(DELAY_S)
    return row


def run_set(client, set_name: str, score, rows: list[dict], message_key: str = "message",
            label: str | None = None, file_name: str | None = None) -> list[dict]:
    """Run every item of one gold set. Adds the rows to `rows` and returns just this set's rows."""
    items = load_set(file_name or set_name)
    print(f"\nSET  {label or set_name}  ({len(items)} items)")
    mine = [run_one(client, set_name, item, item[message_key], score) for item in items]
    print(f"  -> {sum(r['pass'] for r in mine)} of {len(mine)} passed")
    rows.extend(mine)
    return mine


def smoke_items() -> list[tuple[dict, tuple]]:
    """The three smoke items with the ending each one must have."""
    found = []
    for set_name, item_id, want_end, want_blocked_at in SMOKE:
        item = next((i for i in load_set(set_name) if i["id"] == item_id), None)
        if item is None:
            raise EvalError(f"item {item_id} is missing from the {set_name} gold set")
        found.append((item, (want_end, want_blocked_at)))
    return found


def _expect(want: tuple):
    def score(_item, result):
        got = outcome(result["events"])
        return {"pass": got == want,
                "notes": [] if got == want else [f"expected {want[0]} at {want[1]}, got {got[0]} at {got[1]}"]}
    return score


def gate_health(client) -> tuple[bool, str]:
    print("\nGATE 1  HEALTH")
    reply = client.get("/health")
    body = reply.json()
    for name, value in body.items():
        if name not in ("status", "model"):
            print(f"  {name:<9}{value}")
    passed = reply.status_code == 200
    print(f"  -> {'pass' if passed else 'FAIL'} (HTTP {reply.status_code}, model {body.get('model')})\n")
    return passed, str(body.get("model"))


def gate_smoke(client, rows: list[dict]) -> bool:
    print("\nGATE 2  SMOKE")
    mine = [run_one(client, "smoke", item, item["message"], _expect(want)) for item, want in smoke_items()]
    rows.extend(mine)
    passed = all(r["pass"] and not r.get("event_problems") for r in mine)
    print(f"  -> {'pass' if passed else 'FAIL'}")
    return passed


def run_all_sets(client, rows: list[dict]) -> tuple[bool, float | None]:
    """The seven sets in the Stage 10 order.

    Returns (customer_orders unchanged?, the shortest plant-to-ask wait in seconds).
    """
    # Memory pairs, first half. Clear the pair users' memories, then plant all ten facts.
    pairs = load_set("memory")
    pair_users = sorted({USERS[p["user"]] for p in pairs})
    print(f"\nClearing Mem0 for {len(pair_users)} memory-pair users...")
    clear_mem0(pair_users)
    plants = run_set(client, "memory_plant", score_plant, rows, "plant", "memory pairs: PLANT", "memory")
    planted = {row["id"]: row for row in plants}

    # These four fill the waiting time. None of them depends on the others.
    run_set(client, "attack", score_must_block, rows)
    run_set(client, "offtopic", score_must_block, rows)
    run_set(client, "probe", score_probe, rows)
    run_set(client, "legit", score_legit, rows)

    # The legitimate set wrote to actions_log, so start the order and action sets from the seed
    # again (EVALS 3.2). Then photograph customer_orders before and after (EVALS 3.6, T-MUTATE).
    print("\nResetting the database before the order and action sets...")
    reset_db()
    before = snapshot_orders()
    run_set(client, "order", score_order, rows)
    run_set(client, "action", score_action, rows)
    orders_unchanged = snapshot_orders() == before
    print(f"\ncustomer_orders after the order and action sets: {'unchanged' if orders_unchanged else 'CHANGED'}")

    # Memory pairs, second half. Every ask must come at least MEM_WAIT_S after its own plant.
    last_plant = max((row.get("finished_at", 0) for row in plants), default=0)
    still_to_wait = MEM_WAIT_S - (time.monotonic() - last_plant)
    if still_to_wait > 0:
        print(f"\nWaiting {still_to_wait:.0f} s more so every plant is at least {MEM_WAIT_S} s old...")
        time.sleep(still_to_wait)

    def ask_score(item, result):
        scored = score_ask(item, result)
        if not planted[item["id"]]["pass"]:   # no plant, no pair: it cannot count as a hit
            scored["pass"] = False
            scored["notes"].insert(0, "its plant turn did not finish")
        return scored

    asks = run_set(client, "memory", ask_score, rows, "ask", "memory pairs: ASK")
    # Measured, not assumed: the shortest time any ask came after its own plant (T-MEM-WAIT).
    waits = [ask["started_at"] - planted[ask["id"]]["finished_at"]
             for ask in asks if "finished_at" in planted[ask["id"]]]
    return orders_unchanged, (min(waits) if waits else None)


# The sets a quick run can pick from, and how each one is scored.
QUICK_SETS = {"attack": score_must_block, "offtopic": score_must_block, "probe": score_probe,
              "legit": score_legit, "order": score_order, "action": score_action}


def run_memory_pairs(client, rows: list[dict]) -> None:
    """The memory pairs on their own: clear, plant, wait MEM_WAIT_S, ask. Used by quick runs."""
    pairs = load_set("memory")
    print("\nClearing Mem0 for the memory-pair users...")
    clear_mem0(sorted({USERS[p["user"]] for p in pairs}))
    plants = {r["id"]: r for r in run_set(client, "memory_plant", score_plant, rows, "plant",
                                          "memory pairs: PLANT", "memory")}
    print(f"\nWaiting {MEM_WAIT_S} s so Mem0 can store the memories (T-MEM-WAIT)...")
    time.sleep(MEM_WAIT_S)

    def ask_score(item, result):
        scored = score_ask(item, result)
        if not plants[item["id"]]["pass"]:
            scored["pass"] = False
            scored["notes"].insert(0, "its plant turn did not finish")
        return scored

    run_set(client, "memory", ask_score, rows, "ask", "memory pairs: ASK")


def quick_run(names: list[str]) -> int:
    """Run only the named sets. Prints scores and latency; writes no report."""
    unknown = [n for n in names if n not in QUICK_SETS and n != "memory"]
    if unknown:
        print(f"Unknown set name(s): {', '.join(unknown)}. Choose from: {', '.join(QUICK_SETS)}, memory")
        return 2
    started_utc, rows = datetime.now(UTC), []
    try:
        with open_client() as client:
            if not gate_health(client)[0]:
                print("Stopped: a service is down.")
                return 2
            print("Resetting the database...")
            reset_db()
            for name in names:
                if name == "memory":
                    run_memory_pairs(client, rows)
                else:
                    run_set(client, name, QUICK_SETS[name], rows)
        print(f"\nWaiting {SPAN_SETTLE_S} s for the last spans to reach Phoenix, then reading the traces...")
        time.sleep(SPAN_SETTLE_S)
        add_trace_checks(rows, wait_for_spans({r["trace_id"] for r in rows if r.get("trace_id")}, started_utc))
    except httpx.ConnectError:
        print(f"Cannot reach a service the eval needs. Is the web server up at {BASE_URL}, and Phoenix too?")
        return 2
    except EvalError as exc:
        print(f"The eval could not go on: {exc}")
        return 2

    print("\nQUICK RUN  (not evidence: no report was written)")
    for name in names:
        mine = [r for r in rows if r["set"] == name]
        failed = [r for r in mine if not r["pass"]]
        answered = [r["ms"] for r in mine if r.get("terminated") in ("done", "cap")]
        line = f"  {name:<9}{len(mine) - len(failed)} of {len(mine)} passed"
        if answered:
            line += f"   answered turns: p50 {percentile(answered, 50)} ms, p95 {percentile(answered, 95)} ms"
        print(line)
        for row in failed:   # the trace notes are only known now, so print the reasons again
            print(f"        {row['id']}: {'; '.join(row['notes']) or 'failed'}")
    return 0


def run_cli_check(rows: list[dict]) -> None:
    """G-EVENTS: the event rules must hold for the CLI too, so the smoke items also go through it."""
    print("\nCLI  the three smoke items through `support.cli --events`")
    for item, want in smoke_items():
        rows.append(run_one(None, "cli_events", item, item["message"], _expect(want), via_cli=True))


def print_gate(name: str, failures: list[str], warnings: list[str]) -> None:
    print(f"\n{name}")
    for line in failures:
        print(f"  FAIL     {line}")
    for line in warnings:
        print(f"  warning  {line}")
    print(f"  -> {'FAIL' if failures else 'pass'}" + (f" ({len(warnings)} warning(s))" if warnings else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="The eval runner. With no option it does the full run.")
    parser.add_argument("--only", help="quick run of these sets only, comma-separated (for example: order)")
    args = parser.parse_args()
    if args.only:
        return quick_run([name.strip() for name in args.only.split(",") if name.strip()])

    start, started_utc = time.monotonic(), datetime.now(UTC)
    rows: list[dict] = []
    try:
        static_failures, static_warnings = gate_static()
        print_gate("GATE 0  STATIC", static_failures, static_warnings)
        if static_failures:
            print("Stopped: gate 0 failed, so nothing after it ran.")
            return 2
        with open_client() as client:
            healthy, model = gate_health(client)
            if not healthy:
                print("Stopped: gate 1 failed, so nothing after it ran.")
                return 2
            moved = archive_runs()
            print(f"Moved {moved} old run log(s) into runs/archive/. Resetting the database...")
            reset_db()
            if not gate_smoke(client, rows):
                print("Stopped: gate 2 failed, so the seven sets did not run.")
                return 2
            orders_unchanged, mem_wait = run_all_sets(client, rows)
        run_cli_check(rows)

        print(f"\nWaiting {SPAN_SETTLE_S} s for the last spans to reach Phoenix, then reading the traces...")
        time.sleep(SPAN_SETTLE_S)
        spans = wait_for_spans({r["trace_id"] for r in rows if r.get("trace_id")}, started_utc)
        add_trace_checks(rows, spans)
    except httpx.ConnectError:
        print(f"Cannot reach a service the eval needs. Is the web server up at {BASE_URL}, and Phoenix too?")
        return 2
    except EvalError as exc:
        print(f"The eval could not go on: {exc}")
        return 2

    trajectory_failures, trajectory_warnings = gate_trajectory(rows)
    print_gate("GATE 3  TRAJECTORY", trajectory_failures, trajectory_warnings)

    metrics = build_metrics(rows, orders_unchanged, mem_wait)
    missed = [name for name, m in metrics.items() if not m["pass"]]
    print("\nGATE 4  EVAL")
    for name, m in metrics.items():
        print(f"  {'pass' if m['pass'] else 'FAIL'}  {name:<20} {m['value']!s:>8}   target {m['target']:<10} n={m['n']}")
    print(f"  -> {'FAIL: ' + ', '.join(missed) if missed else 'pass'}")

    warnings = static_warnings + trajectory_warnings
    gates = {
        "0_static": {"pass": True, "warnings": static_warnings},
        "1_health": {"pass": True},
        "2_smoke": {"pass": True},
        "3_trajectory": {"pass": not trajectory_failures, "failures": trajectory_failures,
                         "warnings": trajectory_warnings},
        "4_eval": {"pass": not missed, "missed": missed},
        "5_human": {"pass": None, "note": "a person reads the two turns named under trajectories"},
    }
    report = write_report(rows, metrics, gates, model)

    failed_items = [f"{r['id']}({r['set']})" for r in rows if not r["pass"]]
    crossed = [name for name, value in report["red_lines"].items() if value]
    print(f"\nItems that failed: {', '.join(failed_items) or 'none'}")
    print(f"Red lines crossed: {', '.join(crossed) or 'none'}")
    print("\nGATE 5  HUMAN  read these two turns end to end in Phoenix:")
    print(f"  success: {report['trajectories']['success']}")
    print(f"  failing: {report['trajectories']['failing'] or 'none found in runs/failing/ (see EVALS section 6)'}")
    if not report["trajectories"]["failing"]:
        warnings.append("no failing trajectory in runs/failing/")
    print(f"\nWrote {REPORT_PATH}. The run took {(time.monotonic() - start) / 60:.1f} minutes.")

    if trajectory_failures or missed or crossed:
        return 2
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
