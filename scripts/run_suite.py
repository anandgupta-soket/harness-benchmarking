#!/usr/bin/env python3
"""Run the whole task suite against one harness/model and report suite-level
metrics: total run time, failure rate, tokens, estimated cost.

Sequential (one task after another, the cleanest wall-clock number):
  python3 scripts/run_suite.py --harness loop --provider openrouter \\
      --model z-ai/glm-5.3-flash --budget-usd 5

Parallel (N tasks at once; each run is isolated, see run_task.py):
  python3 scripts/run_suite.py --harness loop --provider openrouter \\
      --model z-ai/glm-5.3-flash --jobs 4 --budget-usd 5

Each task is a separate `run_task.py` process; its row is appended to
runs/runs.csv as usual, and one summary row goes to runs/suites.csv.

Reported times:
- total wall time: stopwatch around the whole batch (what you wait).
- serial-equivalent: sum of every task's own elapsed time, i.e. roughly how
  long the same batch would take run one-by-one. With --jobs 1 the two are
  about equal; with --jobs N the ratio is the parallel speedup.

Budget guard: cost is estimated from token counts and OpenRouter list
pricing (auto-fetched when --provider is openrouter, or given with
--price-in-per-m / --price-out-per-m). Once estimated spend reaches
--budget-usd no *new* task is started; tasks already running finish, so a
parallel run can overshoot by up to --jobs tasks' worth. Cache-discounted
tokens aren't broken out, so the estimate is an upper bound.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from run_task import HARNESS_CHOICES, ROOT, append_csv_row, file_lock
from validate_tasks import load_suite_index, parse_simple_yaml

SUITE_COLUMNS = (
    "suite_id",
    "date",
    "harness",
    "model",
    "jobs",
    "tasks_total",
    "tasks_run",
    "passed",
    "failed",
    "skipped_budget",
    "failure_rate_pct",
    "total_wall_s",
    "serial_equiv_s",
    "total_tokens",
    "tool_calls_total",
    "est_cost_usd",
    "budget_usd",
)

RUN_TASK = Path(__file__).resolve().parent / "run_task.py"


@dataclass
class TaskResult:
    task_id: str
    status: str  # pass | fail | error | skipped
    elapsed_s: float
    tokens: int
    tool_calls: int
    cost: float
    row: dict[str, str] | None


def openrouter_prices(model: str) -> tuple[float, float] | None:
    """(input, output) USD per token from OpenRouter's public model list."""
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return None
    for m in data.get("data", []):
        if m.get("id") == model:
            pricing = m.get("pricing") or {}
            try:
                return float(pricing["prompt"]), float(pricing["completion"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def to_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def task_ids(only: set[str] | None) -> list[str]:
    ids = []
    for path in load_suite_index():
        if not path.is_file():
            continue
        tid = str(parse_simple_yaml(path.read_text(encoding="utf-8")).get("id", "")).strip()
        if tid and (not only or tid in only):
            ids.append(tid)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", default="loop", choices=HARNESS_CHOICES)
    ap.add_argument("--provider", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--jobs", type=int, default=1, help="tasks at once; 1 = strictly sequential (default)")
    ap.add_argument("--tasks", default="", help="comma-separated task ids to run (default: whole suite)")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="stop starting new tasks at this estimated spend (0 = no cap)")
    ap.add_argument("--price-in-per-m", type=float, default=None, help="USD per 1M input tokens (overrides auto lookup)")
    ap.add_argument("--price-out-per-m", type=float, default=None, help="USD per 1M output tokens (overrides auto lookup)")
    ap.add_argument("--output", default=str(ROOT / "runs" / "runs.csv"), help="per-task CSV to append to")
    ap.add_argument("--suites-csv", default=str(ROOT / "runs" / "suites.csv"), help="suite summary CSV to append to")
    ap.add_argument("--list", action="store_true", help="print the tasks that would run and exit")
    args = ap.parse_args()

    only = {t.strip() for t in args.tasks.split(",") if t.strip()} or None
    ids = task_ids(only)
    if only and (missing := only - set(ids)):
        raise SystemExit(f"unknown task id(s): {', '.join(sorted(missing))}")
    if args.list:
        print("\n".join(ids))
        return 0
    jobs = max(1, args.jobs)

    price_in = price_out = None
    if args.price_in_per_m is not None and args.price_out_per_m is not None:
        price_in, price_out = args.price_in_per_m / 1e6, args.price_out_per_m / 1e6
    elif args.provider == "openrouter" and args.model:
        prices = openrouter_prices(args.model)
        if prices:
            price_in, price_out = prices
    if args.budget_usd and price_in is None:
        raise SystemExit(
            "--budget-usd needs token prices to estimate spend, and none could be found for this "
            "provider/model - pass --price-in-per-m and --price-out-per-m (or drop the budget cap)."
        )
    price_note = (
        f"${price_in * 1e6:.2f}/M in, ${price_out * 1e6:.2f}/M out" if price_in is not None else "unknown (cost not estimated)"
    )

    suite_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    mode = "sequential" if jobs == 1 else f"parallel x{jobs}"
    print(
        f"suite {suite_id}: {len(ids)} tasks, {args.harness} / {args.model or '(default model)'}, {mode}, "
        f"pricing {price_note}, budget {'$%g' % args.budget_usd if args.budget_usd else 'none'}",
        file=sys.stderr,
    )

    tmp = Path(tempfile.mkdtemp(prefix=f"suite-{suite_id}-"))
    lock = threading.Lock()
    state = {"spend": 0.0, "done": 0}
    results: dict[str, TaskResult] = {}

    def run_one(tid: str) -> None:
        with lock:
            over = bool(args.budget_usd) and state["spend"] >= args.budget_usd
        if over:
            with lock:
                state["done"] += 1
                results[tid] = TaskResult(tid, "skipped", 0.0, 0, 0, 0.0, None)
                print(f"[{state['done']}/{len(ids)}] {tid:11s} SKIPPED (budget reached)", file=sys.stderr)
            return

        out_csv = tmp / f"{tid}.csv"
        cmd = [sys.executable, str(RUN_TASK), tid, "--harness", args.harness, "--output", str(out_csv)]
        if args.provider:
            cmd += ["--provider", args.provider]
        if args.model:
            cmd += ["--model", args.model]
        t0 = time.perf_counter()
        subprocess.run(cmd, capture_output=True, text=True, check=False)
        elapsed = time.perf_counter() - t0

        row = None
        if out_csv.is_file():
            with out_csv.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            row = rows[-1] if rows else None
        if row is None:
            status, tokens, cost, tools = "error", 0, 0.0, 0
        else:
            tokens = to_int(row.get("total_tokens"))
            tools = to_int(row.get("tool_calls"))
            cost = (
                to_int(row.get("input_tokens")) * price_in + to_int(row.get("output_tokens")) * price_out
                if price_in is not None
                else 0.0
            )
            status = "pass" if row.get("success", "").strip().lower() == "true" else "fail"
            append_csv_row(Path(args.output), row)
        with lock:
            state["spend"] += cost
            state["done"] += 1
            results[tid] = TaskResult(tid, status, elapsed, tokens, tools, cost, row)
            print(
                f"[{state['done']}/{len(ids)}] {tid:11s} {status.upper():5s} {elapsed:6.1f}s "
                f"{tokens:>8,} tok  {tools:>3} tools  ${cost:.4f}  (running ${state['spend']:.3f})",
                file=sys.stderr,
            )

    batch_start = time.perf_counter()
    if jobs == 1:
        for tid in ids:
            run_one(tid)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(run_one, ids))
    total_wall = time.perf_counter() - batch_start

    ran = [r for r in results.values() if r.status != "skipped"]
    passed = sum(1 for r in ran if r.status == "pass")
    failed = len(ran) - passed
    skipped = sum(1 for r in results.values() if r.status == "skipped")
    serial_equiv = sum(r.elapsed_s for r in ran)
    total_tokens = sum(r.tokens for r in ran)
    total_tools = sum(r.tool_calls for r in ran)
    spend = sum(r.cost for r in ran)
    fail_rate = 100.0 * failed / len(ran) if ran else 0.0

    summary = {
        "suite_id": suite_id,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "harness": args.harness,
        "model": args.model,
        "jobs": jobs,
        "tasks_total": len(ids),
        "tasks_run": len(ran),
        "passed": passed,
        "failed": failed,
        "skipped_budget": skipped,
        "failure_rate_pct": f"{fail_rate:.1f}",
        "total_wall_s": f"{total_wall:.1f}",
        "serial_equiv_s": f"{serial_equiv:.1f}",
        "total_tokens": total_tokens,
        "tool_calls_total": total_tools,
        "est_cost_usd": f"{spend:.4f}",
        "budget_usd": f"{args.budget_usd:g}" if args.budget_usd else "",
    }
    suites_path = Path(args.suites_csv)
    suites_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock("suites-csv.lock"):
        if suites_path.is_file() and suites_path.stat().st_size > 0:
            with suites_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                old_header, old_rows = list(reader.fieldnames or []), list(reader)
            if old_header != list(SUITE_COLUMNS):  # older file: add new columns, keep rows
                with suites_path.open("w", encoding="utf-8", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=SUITE_COLUMNS, extrasaction="ignore")
                    w.writeheader()
                    for r in old_rows:
                        w.writerow({c: r.get(c, "") or "" for c in SUITE_COLUMNS})
        new_file = not suites_path.is_file() or suites_path.stat().st_size == 0
        with suites_path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SUITE_COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(summary)

    walls = [r.elapsed_s for r in ran]
    print("", file=sys.stderr)
    print(f"=== suite {suite_id} ({args.harness}, {mode}) ===")
    print(f"tasks run:        {len(ran)}/{len(ids)}" + (f"  ({skipped} skipped: budget)" if skipped else ""))
    print(f"passed / failed:  {passed} / {failed}   failure rate {fail_rate:.1f}%")
    print(f"total wall time:  {total_wall:.1f}s  ({total_wall / 60:.1f} min)")
    print(f"serial-equivalent:{serial_equiv:>8.1f}s  (sum of each task's elapsed; speedup x{serial_equiv / total_wall:.2f})" if total_wall else "")
    if walls:
        print(f"per task:         median {statistics.median(walls):.1f}s, max {max(walls):.1f}s")
    print(f"tokens:           {total_tokens:,}")
    print(f"tool calls:       {total_tools:,}" + (f"  (median {statistics.median([r.tool_calls for r in ran]):g} per task)" if ran else ""))
    print(f"est. cost:        ${spend:.4f}" + (f"  of ${args.budget_usd:g} budget" if args.budget_usd else ""))
    for r in sorted(ran, key=lambda r: r.task_id):
        if r.status != "pass":
            note = (r.row or {}).get("notes", "no row logged")[:100]
            print(f"  {r.status.upper()}: {r.task_id} - {note}")
    return 0 if failed == 0 and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
