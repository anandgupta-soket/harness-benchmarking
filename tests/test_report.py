from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq

from bench.report import generate_report


def _json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _trial(root: Path, run: str, aid: str, slot: str, seq: int, outcome: str,
           stage: str = "scored", *, valid: bool = True, cpu: float | None = 4.0,
           tokens: dict | None = None) -> None:
    td = root / "runs" / run / "trials" / aid
    profile = int(slot.split(".")[2])
    harness = slot.split(".")[3]
    cgroup = {"valid": valid, "cpu_seconds": cpu, "peak_memory_bytes": 1000 * seq,
              "sampled_peak_rss_bytes": 900 * seq, "sampled_peak_working_set_bytes": 800 * seq,
              "sampled_peak_usage_bytes": 1100 * seq, "n_samples": 5}
    _json(td / "result.json", {"run_id": run, "attempt_id": aid, "task_id": "BUG-01", "harness": harness,
                               "profile_mib": profile, "stage": stage, "complete": True,
                               "wall_seconds": 9.5, "cgroup": cgroup, "usage": tokens})
    _json(td / "outcome.json", {"attempt_id": aid, "slot_id": slot, "outcome": outcome,
                                "stage": stage, "task": "BUG-01", "harness": harness,
                                "profile_mib": profile, "rep": 1, "seq": seq,
                                "verification": {"passed": outcome == "passed"}, "detail": "<unsafe & detail>"})
    _json(td / "verify" / "verdict.json", {"passed": outcome == "passed", "checks": [], "errors": []})


def _config(root: Path, run: str, *, tasks=("BUG-01",), harnesses=("loop", "pi"), profiles=(512, 1024)) -> None:
    run_dir = root / "runs" / run
    _json(run_dir / "config.json", {
        "experiment": {"repetitions": 1, "memory_mib": list(profiles)},
        "matrix": {"tasks": list(tasks), "harnesses": list(harnesses)},
    })
    planned = [{"slot_id": f"r1.{task}.{profile}.{harness}", "rep": 1, "task": task,
                "profile_mib": profile, "harness": harness, "status": "pending", "stage": "scored"}
               for task in tasks for profile in profiles for harness in harnesses]
    _json(run_dir / "manifest.json", {"stage": "scored", "planned_slots": planned})


def test_report_keeps_planned_slots_retries_and_unknown_metrics(tmp_path: Path) -> None:
    export, out = tmp_path / "export", tmp_path / "report"
    _config(export, "run-a")
    slot = "r1.BUG-01.512.loop"
    _trial(export, "run-a", "a1", slot, 1, "incorrect", cpu=12.0,
           tokens={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    _trial(export, "run-a", "a2", slot, 2, "passed", cpu=8.0,
           tokens={"input_tokens": 20, "output_tokens": 6, "total_tokens": 26})
    # This task has a completed pass but invalid cgroup evidence; resources and CPU remain unknown.
    _trial(export, "run-a", "a3", "r1.BUG-01.1024.loop", 1, "passed", valid=False,
           tokens=None)

    summary = generate_report(export, out)
    groups = {(g["harness"], g["profile_mib"]): g for g in summary["stages"]["scored"]}
    retry_group = groups[("loop", 512)]
    assert retry_group["denominator_source"] == "manifest"
    assert retry_group["planned_slots"] == 1
    assert retry_group["primary_successes"] == 0
    assert retry_group["single_attempt_success_rate"] == 0
    assert retry_group["at_least_once_success_rate"] == 1
    assert retry_group["replacement_attempts"] == 1
    assert retry_group["cpu_seconds"] == {"known_total": 20.0, "known_attempts": 2,
                                           "unknown_attempts": 0, "per_at_least_once_success": 20.0}
    assert retry_group["tokens"]["total_tokens"]["known_total"] == 41
    assert retry_group["tokens"]["total_tokens"]["unknown_attempts"] == 0
    assert retry_group["resources"]["peak_memory_bytes"] == {"median": 1500.0, "sample_count": 2, "missing_count": 0}

    invalid_group = groups[("loop", 1024)]
    assert invalid_group["primary_successes"] == 1
    assert invalid_group["resources"]["peak_memory_bytes"]["median"] is None
    assert invalid_group["cpu_seconds"]["known_total"] is None
    assert invalid_group["tokens"]["total_tokens"]["unknown_attempts"] == 1

    pending_group = groups[("pi", 512)]
    assert pending_group["planned_slots"] == 1
    assert pending_group["started_slots"] == 0
    assert pending_group["slot_outcomes"] == {"pending": 1}
    assert summary["planned_slot_counts"]["pending"] == 2
    assert summary["outcome_counts"]["passed"] == 2
    assert (out / "report.html").is_file() and (out / "summary.json").is_file()
    assert (out / "results.parquet").is_file()
    assert pq.read_table(out / "results.parquet").num_rows == 5
    with (out / "results.csv").open() as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 5
    html = (out / "report.html").read_text()
    assert "&lt;unsafe &amp; detail&gt;" not in html  # details are not interpolated into HTML
    assert "https://" not in html


def test_sqlite_denominator_status_and_stage_separation(tmp_path: Path) -> None:
    export, out = tmp_path / "export", tmp_path / "report"
    run = "protocol-run"
    _config(export, run, tasks=("BUG-01",), harnesses=("loop",), profiles=(2048,))
    _json(export / "runs" / run / "outcome.json", {"stage": "protocol"})
    state_dir = export / "state"
    state_dir.mkdir(parents=True)
    db = sqlite3.connect(state_dir / f"{run}.db")
    db.executescript("""
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
      CREATE TABLE slots(slot_id TEXT PRIMARY KEY, rep INT, task TEXT, profile_mib INT, harness TEXT, cell INT, ord INT, status TEXT);
      CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, slot_id TEXT, seq INT, state TEXT, outcome TEXT, detail TEXT, job_name TEXT, verifier_job TEXT, created_at REAL, updated_at REAL, finished_at REAL, replaces TEXT, replace_reason TEXT, stage TEXT);
      INSERT INTO meta VALUES('stage','protocol');
      INSERT INTO slots VALUES('r1.BUG-01.2048.loop',1,'BUG-01',2048,'loop',0,0,'settled');
      INSERT INTO slots VALUES('r2.BUG-01.2048.loop',2,'BUG-01',2048,'loop',1,0,'cancelled');
      INSERT INTO attempts VALUES('attempt-a','r1.BUG-01.2048.loop',1,'done','passed','ok','job-a',NULL,1,2,3,NULL,NULL,'protocol');
    """)
    db.commit()
    db.close()
    _trial(export, run, "attempt-a", "r1.BUG-01.2048.loop", 1, "passed", stage="protocol")
    # An unrelated scored run must get a separate report section.
    _config(export, "scored-run", tasks=("BUG-01",), harnesses=("loop",), profiles=(512,))
    _trial(export, "scored-run", "scored-a", "r1.BUG-01.512.loop", 1, "incorrect")

    summary = generate_report(export, out)
    assert set(summary["stages"]) == {"protocol", "scored"}
    protocol = summary["stages"]["protocol"][0]
    assert protocol["denominator_source"] == "sqlite"
    assert protocol["planned_slots"] == 2
    assert protocol["slot_outcomes"] == {"cancelled": 1, "passed": 1}
    scored = summary["stages"]["scored"][0]
    assert scored["planned_slots"] == 1
    assert scored["primary_successes"] == 0


def test_html_escapes_untrusted_harness_and_run_names(tmp_path: Path) -> None:
    export, out = tmp_path / "export", tmp_path / "report"
    run = "<img src=x onerror=alert(1)>"
    _config(export, run, tasks=("BUG-01",), harnesses=("<script>alert(1)</script>",), profiles=(512,))
    _trial(export, run, "a1", "r1.BUG-01.512.<script>alert(1)</script>", 1, "passed")
    generate_report(export, out)
    html = (out / "report.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
