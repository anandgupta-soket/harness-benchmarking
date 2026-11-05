#!/usr/bin/env python3
"""Aggregate benchmark CSV runs into a markdown scorecard.

Usage:
  python3 scripts/aggregate_scorecard.py --input runs/runs.csv
  python3 scripts/aggregate_scorecard.py -i runs/sample-runs.csv -o runs/scorecard.md
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_COLUMNS = (
    "run_id",
    "date",
    "harness",
    "model",
    "task_id",
    "repo_sha",
    "success",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "t_first_token_ms",
    "t_last_token_ms",
    "wall_clock_ms",
    "assistant_turns",
    "tool_calls",
    "retries",
    "notes",
)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "pass", "success"}


def parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_cpu_seconds(raw: dict[str, str]) -> float | None:
    """user + system CPU seconds; None if neither column is present/parseable
    (optional columns: absent in CSVs written before CPU tracking existed)."""
    total, seen = 0.0, False
    for col in ("cpu_user_s", "cpu_sys_s"):
        value = (raw.get(col) or "").strip()
        if not value:
            continue
        try:
            total += float(value)
            seen = True
        except ValueError:
            pass
    return total if seen else None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def fmt_num(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if suffix == "ms":
        if value >= 60_000:
            return f"{value / 60_000:.1f}m"
        if value >= 1_000:
            return f"{value / 1_000:.1f}s"
        return f"{value:.0f}ms"
    if suffix == "tok":
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}k"
        return f"{value:.0f}"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def fmt_pct(num: float, den: float) -> str:
    if den == 0:
        return "—"
    return f"{100.0 * num / den:.1f}%"


@dataclass
class RunRow:
    run_id: str
    date: str
    harness: str
    model: str
    task_id: str
    repo_sha: str
    success: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    t_first_token_ms: int | None
    t_last_token_ms: int | None
    wall_clock_ms: int | None
    peak_rss_kb: int | None
    cpu_seconds: float | None
    assistant_turns: int | None
    tool_calls: int | None
    retries: int | None
    notes: str


@dataclass
class HarnessStats:
    total: int = 0
    successes: int = 0
    wall_all: list[float] = field(default_factory=list)
    wall_success: list[float] = field(default_factory=list)
    turns_success: list[float] = field(default_factory=list)
    tokens_success: list[float] = field(default_factory=list)
    rss_success: list[float] = field(default_factory=list)
    cpu_success: list[float] = field(default_factory=list)
    tools_success: list[float] = field(default_factory=list)
    first_token_success: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def load_rows(path: Path) -> list[RunRow]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise SystemExit(f"empty CSV: {path}")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise SystemExit(f"missing columns in {path}: {', '.join(missing)}")

        rows: list[RunRow] = []
        for i, raw in enumerate(reader, start=2):
            if not any(v and v.strip() for v in raw.values()):
                continue
            try:
                rows.append(
                    RunRow(
                        run_id=raw["run_id"].strip(),
                        date=raw["date"].strip(),
                        harness=raw["harness"].strip(),
                        model=raw["model"].strip(),
                        task_id=raw["task_id"].strip(),
                        repo_sha=raw["repo_sha"].strip(),
                        success=parse_bool(raw["success"]),
                        input_tokens=parse_int(raw["input_tokens"]),
                        output_tokens=parse_int(raw["output_tokens"]),
                        total_tokens=parse_int(raw["total_tokens"]),
                        t_first_token_ms=parse_int(raw["t_first_token_ms"]),
                        t_last_token_ms=parse_int(raw["t_last_token_ms"]),
                        wall_clock_ms=parse_int(raw["wall_clock_ms"]),
                        peak_rss_kb=parse_int(raw.get("peak_rss_kb", "")),  # optional: absent in pre-existing CSVs
                        cpu_seconds=parse_cpu_seconds(raw),
                        assistant_turns=parse_int(raw["assistant_turns"]),
                        tool_calls=parse_int(raw["tool_calls"]),
                        retries=parse_int(raw["retries"]),
                        notes=raw["notes"].strip(),
                    )
                )
            except KeyError as e:
                raise SystemExit(f"{path}:{i}: missing field {e}") from e
        return rows


def aggregate(rows: list[RunRow]) -> dict[str, HarnessStats]:
    by_harness: dict[str, HarnessStats] = defaultdict(HarnessStats)
    for row in rows:
        stats = by_harness[row.harness]
        stats.total += 1
        if row.success:
            stats.successes += 1
            if row.wall_clock_ms is not None:
                stats.wall_success.append(float(row.wall_clock_ms))
            if row.assistant_turns is not None:
                stats.turns_success.append(float(row.assistant_turns))
            if row.total_tokens is not None:
                stats.tokens_success.append(float(row.total_tokens))
            if row.peak_rss_kb is not None:
                stats.rss_success.append(float(row.peak_rss_kb))
            if row.cpu_seconds is not None:
                stats.cpu_success.append(row.cpu_seconds)
            if row.tool_calls is not None:
                stats.tools_success.append(float(row.tool_calls))
            if row.t_first_token_ms is not None:
                stats.first_token_success.append(float(row.t_first_token_ms))
        else:
            note = row.notes or "no notes"
            stats.failures.append(f"{row.task_id} ({row.run_id}): {note}")
        if row.wall_clock_ms is not None:
            stats.wall_all.append(float(row.wall_clock_ms))
    return dict(by_harness)


def task_breakdown(rows: list[RunRow]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (harness, task_id) success counts."""
    out: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "success": 0, "tokens": [], "turns": [], "wall": []}
    )
    for row in rows:
        key = (row.harness, row.task_id)
        bucket = out[key]
        bucket["total"] += 1
        if row.success:
            bucket["success"] += 1
            if row.total_tokens is not None:
                bucket["tokens"].append(float(row.total_tokens))
            if row.assistant_turns is not None:
                bucket["turns"].append(float(row.assistant_turns))
            if row.wall_clock_ms is not None:
                bucket["wall"].append(float(row.wall_clock_ms))
    return dict(out)


def load_suites(suites_path: Path | None) -> list[dict[str, str]]:
    if suites_path is None or not suites_path.is_file():
        return []
    with suites_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def render_consolidated(rows: list[RunRow], suites_path: Path | None) -> list[str]:
    """One row per (harness, model) with every metric side by side. Efficiency
    columns use successful runs only (same rule as the rest of the card);
    suite columns come from that combo's most recent *complete* run_suite.py
    batch (all tasks run), so total time and cost reflect a full pass."""
    combos: dict[tuple[str, str], list[RunRow]] = defaultdict(list)
    for r in rows:
        combos[(r.harness, r.model or "—")].append(r)

    suites = load_suites(suites_path)

    def latest_suite(harness: str, model: str) -> dict[str, str] | None:
        match = [
            x
            for x in suites
            if x.get("harness") == harness
            and (x.get("model") or "—") == model
            and x.get("tasks_run") == x.get("tasks_total")
        ]
        return match[-1] if match else None

    def med(vals: list[float]) -> float | None:
        return statistics.median(vals) if vals else None

    order = sorted(combos, key=lambda k: (-sum(r.success for r in combos[k]) / len(combos[k]), k))
    lines = [
        "## Consolidated comparison (harness x model)",
        "",
        "| Harness | Model | Runs | Success | Failure | Median time | p90 time | Median turns | Median tool calls | Median tokens | Median RAM | Median CPU | Full-suite time | Full-suite tool calls | Full-suite cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in order:
        harness, model = key
        rs = combos[key]
        ok = [r for r in rs if r.success]
        wall = med([float(r.wall_clock_ms) for r in ok if r.wall_clock_ms is not None])
        p90 = percentile([float(r.wall_clock_ms) for r in ok if r.wall_clock_ms is not None], 90)
        turns = med([float(r.assistant_turns) for r in ok if r.assistant_turns is not None])
        tools = med([float(r.tool_calls) for r in ok if r.tool_calls is not None])
        toks = med([float(r.total_tokens) for r in ok if r.total_tokens is not None])
        ram = med([r.peak_rss_kb / 1024 for r in ok if r.peak_rss_kb is not None])
        cpu = med([r.cpu_seconds for r in ok if r.cpu_seconds is not None])
        suite = latest_suite(harness, model)
        suite_time = fmt_num(float(suite["total_wall_s"]) * 1000, "ms") if suite else "—"
        suite_tools = (
            fmt_num(float(suite["tool_calls_total"])) if suite and (suite.get("tool_calls_total") or "").strip() else "—"
        )
        suite_cost = f"${float(suite['est_cost_usd']):.3f}" if suite else "—"
        lines.append(
            "| "
            + " | ".join(
                [
                    harness,
                    model,
                    str(len(rs)),
                    fmt_pct(len(ok), len(rs)),
                    fmt_pct(len(rs) - len(ok), len(rs)),
                    fmt_num(wall, "ms"),
                    fmt_num(p90, "ms"),
                    fmt_num(turns),
                    fmt_num(tools),
                    fmt_num(toks, "tok"),
                    f"{ram:.0f} MB" if ram is not None else "—",
                    f"{cpu:.1f}s" if cpu is not None else "—",
                    suite_time,
                    suite_tools,
                    suite_cost,
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "_Time/turns/tool calls/tokens/RAM/CPU are medians over successful runs. Full-suite time and cost come from the "
        "most recent complete `run_suite.py` batch for that harness+model (— if none yet); cost is an upper-bound estimate "
        "from list pricing. Blank (—) means that harness doesn't report the metric or no data yet._"
    )
    lines.append("")
    return lines


def render_suite_section(suites_path: Path | None) -> list[str]:
    """Whole-suite batches from run_suite.py (runs/suites.csv): total run
    time, serial-equivalent time, failure rate, tokens, estimated cost."""
    suites = load_suites(suites_path)
    if not suites:
        return []

    def num(row: dict[str, str], key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except ValueError:
            return 0.0

    lines = [
        "",
        "## Suite runs (total run time)",
        "",
        "| Date | Harness | Model | Mode | Tasks run | Passed | Failure rate | Total time | Serial-equiv. | Speedup | Tokens | Tool calls | Est. cost |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in suites[-10:]:
        wall, serial = num(r, "total_wall_s"), num(r, "serial_equiv_s")
        jobs = int(num(r, "jobs")) or 1
        lines.append(
            "| "
            + " | ".join(
                [
                    r.get("date", ""),
                    r.get("harness", ""),
                    r.get("model", "") or "—",
                    "sequential" if jobs == 1 else f"parallel x{jobs}",
                    f"{int(num(r, 'tasks_run'))}/{int(num(r, 'tasks_total'))}",
                    str(int(num(r, "passed"))),
                    f"{num(r, 'failure_rate_pct'):.1f}%",
                    fmt_num(wall * 1000, "ms"),
                    fmt_num(serial * 1000, "ms"),
                    f"x{serial / wall:.2f}" if wall else "—",
                    fmt_num(num(r, "total_tokens"), "tok"),
                    fmt_num(num(r, "tool_calls_total")) if (r.get("tool_calls_total") or "").strip() else "—",
                    f"${num(r, 'est_cost_usd'):.3f}",
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("_Total time is the batch stopwatch; serial-equivalent is the sum of each task's own elapsed time. Last 10 batches shown. Cost is an upper-bound estimate from list pricing._")
    return lines


def render_scorecard(rows: list[RunRow], source: Path, suites_path: Path | None = None) -> str:
    if not rows:
        return "# Benchmark scorecard\n\nNo runs found.\n"

    by_harness = aggregate(rows)
    by_task = task_breakdown(rows)
    dates = sorted({r.date for r in rows if r.date})
    tasks = sorted({r.task_id for r in rows})
    harnesses = sorted(by_harness.keys(), key=lambda h: (-by_harness[h].successes / max(by_harness[h].total, 1), h))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"# Internal Beta Scorecard",
        "",
        f"- **Generated:** {generated}",
        f"- **Source:** `{source}`",
        f"- **Runs:** {len(rows)}",
        f"- **Tasks:** {len(tasks)}",
        f"- **Date range:** {dates[0]} → {dates[-1]}" if dates else "- **Date range:** —",
        "",
        *render_consolidated(rows, suites_path),
        "## Overall (by harness)",
        "",
        "| Harness | Success rate | Median wall clock | p90 wall clock | Median turns | Median tool calls | Median tokens | Median first token |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for harness in harnesses:
        s = by_harness[harness]
        med_wall = statistics.median(s.wall_success) if s.wall_success else None
        p90_wall = percentile(s.wall_success, 90) if s.wall_success else None
        med_turns = statistics.median(s.turns_success) if s.turns_success else None
        med_tokens = statistics.median(s.tokens_success) if s.tokens_success else None
        med_tools = statistics.median(s.tools_success) if s.tools_success else None
        med_first = statistics.median(s.first_token_success) if s.first_token_success else None
        lines.append(
            "| "
            + " | ".join(
                [
                    harness,
                    fmt_pct(s.successes, s.total),
                    fmt_num(med_wall, "ms"),
                    fmt_num(p90_wall, "ms"),
                    fmt_num(med_turns),
                    fmt_num(med_tools),
                    fmt_num(med_tokens, "tok"),
                    fmt_num(med_first, "ms"),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "_Efficiency metrics (wall clock, turns, tokens, first token) computed on **successful runs only**._",
            "",
            "## Per-task success rate",
            "",
            "| Task | " + " | ".join(harnesses) + " |",
            "|---|" + "|".join(["---:"] * len(harnesses)) + "|",
        ]
    )

    for task_id in tasks:
        cells = [task_id]
        for harness in harnesses:
            bucket = by_task.get((harness, task_id))
            if not bucket or bucket["total"] == 0:
                cells.append("—")
            else:
                cells.append(fmt_pct(bucket["success"], bucket["total"]))
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend(["", "## Token efficiency (successful runs, median total tokens)", ""])
    for harness in harnesses:
        s = by_harness[harness]
        if not s.tokens_success:
            lines.append(f"- **{harness}:** —")
            continue
        med = statistics.median(s.tokens_success)
        p90 = percentile(s.tokens_success, 90)
        lines.append(f"- **{harness}:** median {fmt_num(med, 'tok')}, p90 {fmt_num(p90, 'tok')}")

    lines.extend(["", "## Memory efficiency (successful runs, median peak RSS)", ""])
    for harness in harnesses:
        s = by_harness[harness]
        if not s.rss_success:
            lines.append(f"- **{harness}:** —")
            continue
        med = statistics.median(s.rss_success)
        p90 = percentile(s.rss_success, 90)
        lines.append(f"- **{harness}:** median {fmt_num(med, 'KB')}, p90 {fmt_num(p90, 'KB')}")

    lines.extend(["", "## Tool calls (successful runs)", ""])
    for harness in harnesses:
        s = by_harness[harness]
        if not s.tools_success:
            lines.append(f"- **{harness}:** — (this harness doesn't report tool calls yet)")
            continue
        med = statistics.median(s.tools_success)
        p90 = percentile(s.tools_success, 90)
        total = sum(s.tools_success)
        lines.append(
            f"- **{harness}:** median {fmt_num(med)}, p90 {fmt_num(p90)}, "
            f"total {fmt_num(total)} across {len(s.tools_success)} run(s)"
        )

    lines.extend(["", "## CPU efficiency (successful runs, user + system CPU seconds)", ""])
    for harness in harnesses:
        s = by_harness[harness]
        if not s.cpu_success:
            lines.append(f"- **{harness}:** —")
            continue
        med = statistics.median(s.cpu_success)
        p90 = percentile(s.cpu_success, 90)
        lines.append(f"- **{harness}:** median {med:.1f}s CPU, p90 {p90:.1f}s CPU")

    lines.extend(render_suite_section(suites_path))

    lines.extend(["", "## Top failures", ""])
    failure_lines: list[str] = []
    for harness in harnesses:
        for msg in by_harness[harness].failures[:5]:
            failure_lines.append(f"- **{harness}** — {msg}")
    if failure_lines:
        lines.extend(failure_lines[:15])
    else:
        lines.append("_None recorded._")

    lines.extend(
        [
            "",
            "## Suggested headline order",
            "",
            "1. Success rate",
            "2. Median wall clock (successful)",
            "3. Median turns (successful)",
            "4. Median tokens (successful)",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate benchmark CSV into scorecard")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("runs/runs.csv"),
        help="Input CSV path (default: runs/runs.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output markdown path (default: stdout)",
    )
    parser.add_argument(
        "--suites",
        type=Path,
        default=None,
        help="Suite summary CSV from run_suite.py (default: suites.csv next to the input)",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit(f"input not found: {args.input}")

    rows = load_rows(args.input)
    suites_path = args.suites or args.input.with_name("suites.csv")
    report = render_scorecard(rows, args.input, suites_path)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
