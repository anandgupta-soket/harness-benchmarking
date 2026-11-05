"""Qualification evidence is bound to the complete immutable experiment configuration."""
from __future__ import annotations
import json
import time
from pathlib import Path
from .config import Config

REQUIRED = ("resources", "controls", "protocol", "matrix")


def write_evidence(data: Path, cfg: Config, stage: str, evidence: dict) -> dict:
    if stage not in REQUIRED:
        raise ValueError(f"unknown qualification stage {stage}")
    result = {**evidence, "stage": stage, "config_sha256": cfg.sha256(), "recorded_at": time.time()}
    dest = data / "qualification" / cfg.sha256() / f"{stage}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    tmp.replace(dest)
    return result


def record_qualification(cfg: Config, state, run_dir: Path, stage: str) -> None:
    if stage not in ("protocol", "matrix"):
        return
    attempts = state.all_attempts()
    reasons = []
    if not attempts or state.counts()["slots"].get("pending", 0):
        reasons.append("qualification did not complete all slots")
    for a in attempts:
        if a["outcome"] in ("infra_error", "harness_error", "provider_error", "cancelled", None):
            reasons.append(f"{a['harness']}/{a['task']}: {a['outcome']}")
        if stage == "protocol":
            try:
                r = json.loads((run_dir / "trials" / a["attempt_id"] / "result.json").read_text())
                usage = r.get("usage") or {}
                if (usage.get("total_tokens") or 0) <= 0 or (usage.get("tool_calls") or 0) < 1:
                    reasons.append(f"{a['harness']}: no recorded model usage and executed tool call")
                if not r.get("cgroup", {}).get("valid"):
                    reasons.append(f"{a['harness']}: invalid cgroup measurement")
            except (OSError, ValueError):
                reasons.append(f"{a['harness']}: missing trial evidence")
    evidence = {"passed": not reasons, "reasons": reasons, "run_id": state.meta("run_id"),
                "counts": state.counts(), "scope": "unscored integration smoke; not proof of backend model identity or complete protocol equivalence"}
    write_evidence(run_dir.parent.parent, cfg, stage, evidence)


def require_qualification(data: Path, cfg: Config) -> dict:
    records = {}
    for stage in REQUIRED:
        p = data / "qualification" / cfg.sha256() / f"{stage}.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            raise ValueError(f"qualification {stage} missing; run bench qualify --stage {stage}")
        if d.get("config_sha256") != cfg.sha256() or not d.get("passed"):
            raise ValueError(f"qualification {stage} failed or belongs to another configuration")
        records[stage] = d
    return records
