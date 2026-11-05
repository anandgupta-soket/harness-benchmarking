"""Deterministic reports from exported benchmark runs and optional controller state."""
from __future__ import annotations

import csv
import html
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import HARNESSES

OUTCOMES = ("passed", "incorrect", "oom", "timeout", "harness_error", "provider_error", "infra_error", "cancelled")
RESOURCE_FIELDS = {
    "peak_memory_bytes": "peak_memory_bytes",
    "sampled_peak_rss_bytes": "sampled_peak_rss_bytes",
    "sampled_peak_working_set_bytes": "sampled_peak_working_set_bytes",
    "sampled_peak_usage_bytes": "sampled_peak_usage_bytes",
}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
CSV_FIELDS = (
    "run_id", "attempt_id", "slot_id", "task_id", "harness", "profile_mib", "stage", "rep", "seq",
    "replaces", "is_replacement", "is_primary", "attempt_outcome", "slot_status", "verifier_passed",
    "complete", "timed_out", "cancelled", "cgroup_valid", "wall_seconds", "cpu_seconds",
    "peak_memory_bytes", "sampled_peak_rss_bytes", "sampled_peak_working_set_bytes",
    "sampled_peak_usage_bytes", "n_samples", "input_tokens", "output_tokens", "total_tokens", "detail",
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        val = json.loads(path.read_text(encoding="utf-8"))
        return val if isinstance(val, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _attempt_row(run_id: str, aid: str, trial_dir: Path, db_attempt: dict[str, Any] | None,
                 slot: dict[str, Any] | None, default_stage: str = "scored") -> dict[str, Any]:
    result = _read_json(trial_dir / "result.json") or {}
    outcome_doc = _read_json(trial_dir / "outcome.json") or {}
    verdict = _read_json(trial_dir / "verify" / "verdict.json") or {}
    db_attempt = db_attempt or {}
    identity = {**(slot or {}), **db_attempt, **result, **outcome_doc}
    stage = str(identity.get("stage") or default_stage)
    seq = identity.get("seq") if identity.get("seq") is not None else 1
    try:
        seq = int(seq)
    except (ValueError, TypeError):
        seq = 1
    outcome = outcome_doc.get("outcome") or db_attempt.get("outcome")
    if outcome not in OUTCOMES:
        if result.get("cancelled"):
            outcome = "cancelled"
        elif result.get("timed_out"):
            outcome = "timeout"
        elif result.get("worker_error"):
            outcome = "harness_error"
        elif not result:
            outcome = "missing_artifact"
        elif result.get("complete") is False:
            outcome = "incomplete"
        else:
            outcome = "unclassified"
    cgroup = result.get("cgroup") if isinstance(result.get("cgroup"), dict) else {}
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    resource_ok = cgroup.get("valid") is True
    verifier_passed = verdict.get("passed") if isinstance(verdict.get("passed"), bool) else None
    if not isinstance(verdict.get("passed"), bool):
        vv = outcome_doc.get("verification")
        verifier_passed = vv.get("passed") if isinstance(vv, dict) and isinstance(vv.get("passed"), bool) else None
    task_id = identity.get("task") or identity.get("task_id")
    harness = identity.get("harness")
    profile = identity.get("profile_mib")
    rep = identity.get("rep") or 1
    slot_id = identity.get("slot_id") or db_attempt.get("slot_id")
    if not slot_id and task_id and harness and profile is not None:
        slot_id = f"r{rep}.{task_id}.{profile}.{harness}"
    values: dict[str, Any] = {
        "run_id": run_id,
        "attempt_id": aid,
        "slot_id": slot_id,
        "task_id": task_id,
        "harness": harness,
        "profile_mib": profile,
        "stage": stage,
        "rep": rep,
        "seq": seq,
        "replaces": db_attempt.get("replaces"),
        "is_replacement": seq > 1 or bool(db_attempt.get("replaces")),
        "is_primary": seq == 1,
        "attempt_outcome": outcome,
        "slot_status": (slot or {}).get("status") or db_attempt.get("state"),
        "verifier_passed": verifier_passed,
        "complete": result.get("complete"),
        "timed_out": result.get("timed_out"),
        "cancelled": result.get("cancelled"),
        "cgroup_valid": cgroup.get("valid") if isinstance(cgroup.get("valid"), bool) else None,
        "wall_seconds": _number(result.get("wall_seconds")),
        "cpu_seconds": _number(cgroup.get("cpu_seconds")) if resource_ok else None,
        "n_samples": cgroup.get("n_samples") if resource_ok else None,
        "detail": str(outcome_doc.get("detail") or db_attempt.get("detail") or result.get("worker_error") or ""),
    }
    for field, source in RESOURCE_FIELDS.items():
        values[field] = _number(cgroup.get(source)) if resource_ok else None
    for field in TOKEN_FIELDS:
        values[field] = _number(usage.get(field))
    # Some adapter rows omit total_tokens even though both components exist.
    if values["total_tokens"] is None and values["input_tokens"] is not None and values["output_tokens"] is not None:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def _load_state(db_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    slots: dict[str, dict[str, Any]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    if not db_path.is_file():
        return slots, attempts, meta
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" in tables:
            meta = {str(r[0]): str(r[1]) for r in con.execute("SELECT k,v FROM meta")}
        if "slots" in tables:
            slots = {str(r["slot_id"]): dict(r) for r in con.execute("SELECT * FROM slots")}
        if "attempts" in tables:
            attempts = {str(r["attempt_id"]): dict(r) for r in con.execute("SELECT * FROM attempts")}
        con.close()
    except sqlite3.Error:
        return {}, {}, {}
    return slots, attempts, meta


def _planned_from_config(config: dict[str, Any], stage: str) -> dict[str, dict[str, Any]]:
    ex = config.get("experiment") if isinstance(config.get("experiment"), dict) else {}
    q = config.get("qualification") if isinstance(config.get("qualification"), dict) else {}
    if stage == "protocol":
        item = q.get("protocol") or {}
        tasks, profiles, reps = [item.get("task", "BUG-01")], [item.get("profile_mib", 2048)], 1
        harnesses = list((config.get("matrix") or {}).get("harnesses") or HARNESSES)
    elif stage == "matrix":
        item = q.get("matrix") or {}
        tasks, profiles, reps = item.get("tasks", []), item.get("profiles", []), int(item.get("repetitions", 1))
        harnesses = list(item.get("harnesses") or [])
    else:
        from .tasks import TASK_IDS
        item = config.get("matrix") if isinstance(config.get("matrix"), dict) else {}
        # The scored controller uses every suite task; matrix.tasks only applies
        # to qualification.matrix. An explicit manifest can describe a subset.
        tasks = list(TASK_IDS)
        profiles = ex.get("memory_mib") or []
        reps = int(ex.get("repetitions", 1))
        harnesses = list(item.get("harnesses") or HARNESSES)
    out = {}
    for rep in range(1, reps + 1):
        for task in tasks:
            for profile in profiles:
                for harness in harnesses:
                    key = f"r{rep}.{task}.{profile}.{harness}"
                    out[key] = {"slot_id": key, "rep": rep, "task": task, "profile_mib": profile,
                                "harness": harness, "status": "pending"}
    return out


def _med(values: list[float | int]) -> float | int | None:
    return statistics.median(values) if values else None


def _aggregate(stage: str, harness: str, profile: int, planned_slots: list[dict[str, Any]],
               rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_slot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("slot_id") and row.get("attempt_id"):
            by_slot[str(row["slot_id"])].append(row)
    primary_passes = at_least_once = started = replacement_count = 0
    slot_outcomes: Counter[str] = Counter()
    for slot in planned_slots:
        sid = str(slot["slot_id"])
        tries = sorted(by_slot.get(sid, []), key=lambda r: (int(r.get("seq") or 1), str(r.get("attempt_id") or "")))
        replacement_count += sum(bool(t.get("is_replacement")) for t in tries)
        if tries:
            started += 1
            primary = next((t for t in tries if int(t.get("seq") or 1) == 1), None)
            if primary is not None and primary.get("attempt_outcome") == "passed":
                primary_passes += 1
            if any(t.get("attempt_outcome") == "passed" for t in tries):
                at_least_once += 1
            slot_outcomes[str(primary.get("attempt_outcome") if primary else "missing_primary_attempt")] += 1
        else:
            status = str(slot.get("status") or "pending")
            if status in {"cancelled", "canceled"}:
                slot_outcomes["cancelled"] += 1
            elif status in {"active", "settled"}:
                slot_outcomes["incomplete"] += 1
            else:
                slot_outcomes["pending"] += 1

    group_rows = [r for r in rows if r.get("attempt_id") and r.get("harness") == harness and _to_int(r.get("profile_mib")) == profile and r.get("stage") == stage]
    resource_rows = [r for r in group_rows if r.get("cgroup_valid") is True]
    resources = {}
    for field in RESOURCE_FIELDS:
        vals = [r[field] for r in resource_rows if _number(r.get(field)) is not None]
        resources[field] = {"median": _med(vals), "sample_count": len(vals), "missing_count": len(resource_rows) - len(vals)}
    cpu_values = [r["cpu_seconds"] for r in group_rows if _number(r.get("cpu_seconds")) is not None]
    cpu_total = sum(cpu_values) if cpu_values else None
    completed_successes = at_least_once
    cpu_per_success = cpu_total / completed_successes if cpu_total is not None and completed_successes else None
    token_metrics = {}
    for key in TOKEN_FIELDS:
        nums = [r[key] for r in group_rows if _number(r.get(key)) is not None]
        total = sum(nums) if nums else None
        token_metrics[key] = {"known_total": total, "known_attempts": len(nums),
                              "unknown_attempts": len(group_rows) - len(nums),
                              "per_at_least_once_success": total / completed_successes if total is not None and completed_successes else None}
    planned = len(planned_slots)
    all_outcomes = Counter(str(r.get("attempt_outcome") or "unclassified") for r in group_rows)
    valid_resource_attempts = len(resource_rows)
    return {
        "stage": stage, "harness": harness, "profile_mib": profile,
        "planned_slots": planned, "started_slots": started,
        "primary_successes": primary_passes,
        "single_attempt_success_rate": primary_passes / planned if planned else None,
        "at_least_once_successes": at_least_once,
        "at_least_once_success_rate": at_least_once / planned if planned else None,
        "slot_outcomes": dict(sorted(slot_outcomes.items())),
        "attempt_outcomes": dict(sorted(all_outcomes.items())),
        "replacement_attempts": replacement_count,
        "cpu_seconds": {"known_total": cpu_total, "known_attempts": len(cpu_values),
                        "unknown_attempts": len(group_rows) - len(cpu_values),
                        "per_at_least_once_success": cpu_per_success},
        "tokens": token_metrics,
        "valid_resource_attempts": valid_resource_attempts,
        "resources": resources,
    }


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_html(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    esc = lambda x: html.escape("" if x is None else str(x), quote=True)
    sections: list[str] = []
    for stage, groups in summary["stages"].items():
        tr = []
        for g in groups:
            tr.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in (
                g["run_id"], g["denominator_source"], g["harness"], g["profile_mib"], g["planned_slots"], g["started_slots"],
                g["primary_successes"], _pct(g["single_attempt_success_rate"]),
                g["at_least_once_successes"], _pct(g["at_least_once_success_rate"]),
                g["replacement_attempts"],
                g["resources"]["peak_memory_bytes"]["median"], g["resources"]["sampled_peak_rss_bytes"]["median"],
                g["cpu_seconds"]["known_total"], g["cpu_seconds"]["per_at_least_once_success"],
                g["tokens"]["total_tokens"]["known_total"], g["tokens"]["total_tokens"]["per_at_least_once_success"],
                json.dumps(g["slot_outcomes"], sort_keys=True),
            )) + "</tr>")
        sections.append(f"<h2>{esc(stage)}</h2><table><thead><tr><th>Run</th><th>Denominator source</th><th>Harness</th><th>Profile MiB</th><th>Planned</th><th>Started</th><th>First attempt passes</th><th>First attempt rate</th><th>Ever passed</th><th>At least once rate</th><th>Replacement attempts</th><th>Median peak bytes</th><th>Median sampled RSS bytes</th><th>Known CPU total seconds</th><th>CPU seconds per successful slot</th><th>Known tokens total</th><th>Tokens per successful slot</th><th>Primary slot outcomes</th></tr></thead><tbody>{''.join(tr)}</tbody></table>")
    attempt_rows = []
    for r in rows:
        attempt_rows.append("<tr>" + "".join(f"<td>{esc(r.get(k))}</td>" for k in (
            "stage", "harness", "profile_mib", "task_id", "rep", "seq", "attempt_outcome", "is_primary", "is_replacement", "cpu_seconds", "total_tokens"
        )) + "</tr>")
    body = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Benchmark report</title><style>body{{font:15px system-ui,sans-serif;margin:2rem;color:#17212b}}table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}}th,td{{border:1px solid #c9d2da;padding:.45rem;text-align:left}}th{{background:#edf2f6}}tr:nth-child(even){{background:#f8fafb}}h1{{margin-bottom:.2rem}}.muted{{color:#53606c}}</style>
<h1>Benchmark report</h1><p class="muted">Run export: {esc(summary.get('export_root'))}. Qualification stages are reported separately and are not pooled with scored results.</p>
{''.join(sections)}<h2>Attempt history</h2><table><thead><tr><th>Stage</th><th>Harness</th><th>MiB</th><th>Task</th><th>Rep</th><th>Seq</th><th>Outcome</th><th>Primary</th><th>Replacement</th><th>CPU seconds</th><th>Total tokens</th></tr></thead><tbody>{''.join(attempt_rows)}</tbody></table>
</html>"""
    path.write_text(body, encoding="utf-8")


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def generate_report(export_root: Path, output_dir: Path) -> dict[str, Any]:
    """Generate deterministic HTML/CSV/Parquet/JSON report files from a run export."""
    export_root = Path(export_root).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_root = export_root / "runs"
    state_root = export_root / "state"
    rows: list[dict[str, Any]] = []
    denominator_sources: dict[str, str] = {}
    run_ids = sorted(p.name for p in runs_root.iterdir() if p.is_dir()) if runs_root.is_dir() else []
    for run_id in run_ids:
        run_dir = runs_root / run_id
        config = _read_json(run_dir / "config.json") or {}
        db_slots, db_attempts, db_meta = _load_state(state_root / f"{run_id}.db")
        artifact_paths = {p.name: p for p in (run_dir / "trials").iterdir() if p.is_dir()} if (run_dir / "trials").is_dir() else {}
        manifest = (_read_json(run_dir / "manifest.json") or _read_json(export_root / "manifests" / f"{run_id}.json")
                    or _read_json(export_root / "manifest.json") or {})
        if not config and isinstance(manifest.get("config"), dict):
            config = manifest["config"]
        artifact_stage = None
        for artifact in artifact_paths.values():
            outcome_doc = _read_json(artifact / "outcome.json") or {}
            result_doc = _read_json(artifact / "result.json") or {}
            artifact_stage = outcome_doc.get("stage") or result_doc.get("stage")
            if artifact_stage:
                break
        stage_fallback = (db_meta.get("stage") or manifest.get("stage") or artifact_stage or
                          next((a.get("stage") for a in db_attempts.values() if a.get("stage")), "scored"))
        if db_slots:
            planned = db_slots
            denominator_source = "sqlite"
        elif isinstance(manifest.get("planned_slots", manifest.get("slots")), list):
            planned = {}
            manifest_slots = manifest.get("planned_slots", manifest.get("slots"))
            for i, item in enumerate(manifest_slots):
                if not isinstance(item, dict) or (item.get("stage") and item["stage"] != stage_fallback):
                    continue
                item = {**item, "stage": item.get("stage") or stage_fallback}
                planned[str(item.get("slot_id") or f"manifest-{i}")] = item
            denominator_source = "manifest"
        else:
            planned = _planned_from_config(config, str(stage_fallback))
            denominator_source = "config_manifest"
        denominator_sources[run_id] = denominator_source
        attempts_by_slot: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in db_attempts.values():
            attempts_by_slot[str(a.get("slot_id") or "")].append(a)
        ids = set(artifact_paths) | set(db_attempts)
        for aid in sorted(ids):
            trial_dir = artifact_paths.get(aid, run_dir / "trials" / aid)
            db_a = db_attempts.get(aid)
            outcome_doc = _read_json(trial_dir / "outcome.json") or {}
            slot_id = str((db_a or {}).get("slot_id") or outcome_doc.get("slot_id") or "")
            slot = planned.get(slot_id)
            row = _attempt_row(run_id, aid, trial_dir, db_a, slot, str(stage_fallback))
            rows.append(row)
            if slot_id and slot_id not in planned:
                planned[slot_id] = {"slot_id": slot_id, **{k: row.get(src) for k, src in (("task", "task_id"), ("harness", "harness"), ("profile_mib", "profile_mib"), ("rep", "rep"))}, "status": "active"}
        # With an authoritative DB, it is the full planned denominator. Without one,
        # the frozen config manifest defines it. Missing slots remain explicit rows.
        for slot_id, slot in sorted(planned.items()):
            if attempts_by_slot.get(slot_id) or any(r.get("slot_id") == slot_id for r in rows if r["run_id"] == run_id):
                continue
            status = str(slot.get("status") or "pending")
            outcome = "cancelled" if status in {"cancelled", "canceled"} else ("incomplete" if status in {"active", "settled"} else "pending")
            rows.append({
                "run_id": run_id, "attempt_id": "", "slot_id": slot_id,
                "task_id": slot.get("task"), "harness": slot.get("harness"), "profile_mib": slot.get("profile_mib"),
                "stage": str(slot.get("stage") or db_meta.get("stage") or stage_fallback), "rep": slot.get("rep"), "seq": None,
                "replaces": None, "is_replacement": False, "is_primary": None,
                "attempt_outcome": outcome, "slot_status": status, "verifier_passed": None,
                "complete": None, "timed_out": None, "cancelled": status in {"cancelled", "canceled"},
                "cgroup_valid": None, "wall_seconds": None, "cpu_seconds": None, "n_samples": None,
                "peak_memory_bytes": None, "sampled_peak_rss_bytes": None,
                "sampled_peak_working_set_bytes": None, "sampled_peak_usage_bytes": None,
                "input_tokens": None, "output_tokens": None, "total_tokens": None, "detail": "",
            })

    # Stage-specific group keys keep qualification separate from scored runs.
    stage_groups: dict[str, list[dict[str, Any]]] = {}
    group_keys = sorted({(str(r.get("stage") or "scored"), str(r.get("run_id") or ""), str(r.get("harness") or "unknown"), _to_int(r.get("profile_mib")) or 0) for r in rows})
    for stage, run_id, harness, profile in group_keys:
        matching_rows = [r for r in rows if r.get("stage") == stage and r.get("run_id") == run_id]
        # Only planned slots should determine denominators; artifact-only slots are
        # included as incomplete plans so their actual work is never dropped.
        by_sid: dict[str, dict[str, Any]] = {}
        for row in matching_rows:
            sid = str(row.get("slot_id") or "")
            if sid and row.get("harness") == harness and _to_int(row.get("profile_mib")) == profile:
                by_sid.setdefault(sid, {"slot_id": sid, "task": row.get("task_id"), "harness": harness,
                                        "profile_mib": profile, "status": row.get("slot_status") or "active"})
        attempts = [r for r in rows if r.get("stage") == stage and r.get("run_id") == run_id and r.get("harness") == harness and _to_int(r.get("profile_mib")) == profile and r.get("attempt_id")]
        agg = _aggregate(stage, harness, profile, list(by_sid.values()), attempts)
        agg["run_id"] = run_id
        agg["denominator_source"] = denominator_sources.get(run_id, "artifact_only")
        stage_groups.setdefault(stage, []).append(agg)

    attempts_only = [r for r in rows if r.get("attempt_id")]
    planned_only = [r for r in rows if not r.get("attempt_id")]
    summary = {"export_root": str(export_root), "run_ids": run_ids, "attempt_count": len(attempts_only),
               "slot_count": len({(r.get("run_id"), r.get("slot_id")) for r in rows if r.get("slot_id")}),
               "stages": {k: sorted(v, key=lambda g: (g["run_id"], g["harness"], g["profile_mib"])) for k, v in sorted(stage_groups.items())},
               "outcome_counts": dict(sorted(Counter(str(r.get("attempt_outcome") or "unclassified") for r in attempts_only).items())),
               "planned_slot_counts": dict(sorted(Counter(str(r.get("attempt_outcome") or "pending") for r in planned_only).items()))}
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    with (output_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r.get("run_id") or "", r.get("stage") or "", r.get("slot_id") or "", r.get("seq") or 0, r.get("attempt_id") or "")))
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError("pyarrow is required to write results.parquet; install loop-bench[parquet]") from e
    parquet_rows = [{field: row.get(field) for field in CSV_FIELDS} for row in sorted(rows, key=lambda r: (r.get("run_id") or "", r.get("stage") or "", r.get("slot_id") or "", r.get("seq") or 0, r.get("attempt_id") or ""))]
    pq.write_table(pa.Table.from_pylist(parquet_rows), output_dir / "results.parquet")
    _write_html(output_dir / "report.html", summary, rows)
    return summary
