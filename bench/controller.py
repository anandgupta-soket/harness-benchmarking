"""In-cluster controller: ``python -m bench.controller`` (run as a long-lived Job).

It owns all dispatch decisions. Properties:
* state in SQLite on the PVC; every transition is committed before the corresponding kube
  side effect that is idempotent (Job names derive from the attempt UUID), so restarts
  reconcile instead of duplicating work;
* dispatch obeys the barrier (all harnesses of one task/profile/repetition together),
  <= max_concurrent trial pods, exactly one verifier pod at a time, storage free-space gate;
* pause drains (no new dispatch, running attempts finish); cancel deletes child Jobs;
* the collector runs here, outside every measured cgroup: Job/Pod/event snapshots, pod status
  (the only source of whole-pod OOMKilled), pod log tail, then classification;
* verification runs in a separate clean pod (no secrets) over the safe-copied submission.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import manifests as M
from . import storage
from .config import Config, config_from_frozen
from .kube import Kube, KubeError
from .matrix import stage_matrix
from .outcome import classify, pod_oom_killed
from .state import COLLECTING, DISPATCHING, DONE, RUNNING, VERIFYING, State, job_name_for
from .tasks import get_task, load_tasks

POLL_SECONDS = 5.0
PENDING_TIMEOUT_S = 15 * 60


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))
    os.replace(tmp, p)


class Controller:
    def __init__(self, cfg: Config, kube: Kube, data_dir: Path, run_id: str, stage: str = "scored",
                 clock: Callable[[], float] = time.time):
        self.cfg, self.kube, self.data, self.run_id, self.stage, self.now = cfg, kube, Path(data_dir), run_id, stage, clock
        self.run_dir = self.data / "runs" / run_id
        self.max_conc = int(cfg.data["experiment"]["max_concurrent_trials"])
        self.pvc_size = cfg.data["storage"]["size"]
        self.min_free = float(cfg.data["storage"]["min_free_fraction"])
        (self.data / "state").mkdir(parents=True, exist_ok=True)
        self.state = State(self.data / "state" / f"{run_id}.db")
        self._blocked_logged = False
        self.tasks = {t.id: t for t in load_tasks()}
        self.selector = f"{M.LABEL}/run={run_id}"

    # ------------------------------------------------------------------ setup
    def init(self) -> None:
        slots = stage_matrix(self.cfg.data, self.stage, list(self.tasks))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        created = self.state.init_run(self.run_id, self.cfg.sha256(), slots)
        existing_stage = self.state.meta("stage")
        if existing_stage is not None and existing_stage != self.stage:
            raise ValueError("cannot change an existing experiment stage")
        self.state.set_meta("stage", self.stage)
        self.state.set_meta("scored", "1" if self.stage == "scored" else "0")
        if created:
            _write_json(self.run_dir / "config.json", self.cfg.data)
            (self.run_dir / "config.sha256").write_text(self.cfg.sha256())

    # ------------------------------------------------------------------ control plane
    def control_state(self) -> str:
        cm = self.kube.get_json("configmap", M.control_map_name(self.run_id), check=False)
        st = ((cm or {}).get("data") or {}).get("state", "running")
        return st if st in ("running", "paused", "cancelled") else "paused"   # unknown value => safest (stop dispatch)

    def trial_path(self, aid: str) -> Path:
        return self.run_dir / "trials" / aid

    def timeout_s(self, task_id: str) -> int:
        return self.tasks[task_id].timeout_minutes * 60

    # ------------------------------------------------------------------ one pass
    def step(self) -> str:
        """One reconcile+dispatch iteration. Returns: running | paused | cancelled | done | blocked."""
        self.state.set_meta("heartbeat", str(self.now()))
        (self.run_dir / "heartbeat").write_text(str(self.now()))
        ctl = self.control_state()
        self.state.set_meta("control", ctl)
        if ctl == "cancelled":
            self.cancel_all()
            return "cancelled"
        self.progress_live()
        if self.state.current_cell() is None and not self.state.live_attempts():
            return self.finish_run()
        if ctl == "paused":
            return "paused"
        st = storage.check(self.data, self.pvc_size, self.min_free)
        self.state.set_meta("storage", json.dumps(st.as_dict()))
        if not st.ok:
            if not self._blocked_logged:
                self.state.event("dispatch_blocked", f"storage free {st.free_fraction:.3f} < {self.min_free}")
                self._blocked_logged = True
            self.state.set_meta("dispatch_blocked", "storage")
            return "blocked"
        self._blocked_logged = False
        self.state.set_meta("dispatch_blocked", "")
        for slot in self.state.dispatchable(self.max_conc):
            a = self.state.begin_attempt(slot["slot_id"], stage=self.stage)
            self.advance(a)
        return "running"

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> int:
        self.init()
        while True:
            try:
                r = self.step()
            except KubeError as e:           # transient API/ssh problems must not kill the controller
                self.state.event("kube_error", str(e)[:300])
                r = "running"
            if r in ("done", "cancelled") and not self.state.live_attempts():
                return 0
            sleep(POLL_SECONDS)

    # ------------------------------------------------------------------ attempt state machine
    def progress_live(self) -> None:
        for a in self.state.live_attempts():
            self.advance(self.state.attempt(a["attempt_id"]))

    def advance(self, a) -> None:
        st = a["state"]
        if st == DISPATCHING:
            self.dispatch(a)
        elif st == RUNNING:
            self.check_trial(a)
        elif st == COLLECTING:
            self.collect(a)
        elif st == VERIFYING:
            self.check_verifier(a)

    def dispatch(self, a) -> None:
        aid = a["attempt_id"]
        tdir = self.trial_path(aid)
        tdir.mkdir(parents=True, exist_ok=True)
        os.chmod(tdir, 0o700)                    # agent uid must never reach artifacts
        _write_json(tdir / "attempt.json", {k: a[k] for k in a.keys()})
        job = M.trial_job(self.cfg, self.run_id, aid, a["job_name"], a["task"], a["harness"], a["profile_mib"],
                          self.timeout_s(a["task"]), stage=self.stage)
        created = self.kube.create(job)
        self.state.event("job_created" if created else "job_adopted", a["job_name"], aid)
        self.state.set_state(aid, RUNNING)

    def _job_phase(self, name: str) -> tuple[str, dict | None]:
        j = self.kube.get_json("job", name, check=False)
        if j is None:
            return "missing", None
        s = j.get("status", {})
        if s.get("succeeded"):
            return "succeeded", j
        if s.get("failed") or any(c.get("type") == "Failed" and c.get("status") == "True" for c in s.get("conditions", [])):
            return "failed", j
        return "active", j

    def _pod_for(self, aid: str) -> dict | None:
        pods = self.kube.pods_for(f"{M.LABEL}/attempt={aid},{M.LABEL}/role=trial")
        if not pods:
            return None
        return sorted(pods, key=lambda p: p["metadata"].get("creationTimestamp", ""))[-1]

    def check_trial(self, a) -> None:
        aid = a["attempt_id"]
        phase, job = self._job_phase(a["job_name"])
        tdir = self.trial_path(aid)
        complete = (tdir / "COMPLETE").exists()
        if phase == "missing":
            if complete:
                self.state.set_state(aid, COLLECTING)
                return
            # job vanished and no result: never silently re-dispatch; surface as infra_error (explicit replace)
            self.collect_external(a, None)
            self.state.finish(aid, "infra_error", "Job disappeared before the worker completed")
            return
        pod = self._pod_for(aid)
        if phase in ("succeeded", "failed") or (complete and pod and pod.get("status", {}).get("phase") in ("Succeeded", "Failed")):
            self.state.set_state(aid, COLLECTING)
            return
        if pod and pod.get("status", {}).get("phase") == "Pending":
            age = self.now() - self._ts(pod["metadata"].get("creationTimestamp"))
            if age > PENDING_TIMEOUT_S:
                self.collect_external(a, pod)
                self.kube.delete("job", a["job_name"])
                self.state.finish(aid, "infra_error", f"pod Pending > {PENDING_TIMEOUT_S}s (unschedulable / image pull)")
        # whole-pod OOM can end the container while the Job still shows active for a moment; the next pass sees it

    @staticmethod
    def _ts(s: str | None) -> float:
        if not s:
            return time.time()
        import datetime as dt
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    def collect_external(self, a, pod: dict | None) -> dict | None:
        """Outside-the-workload collector: pod/job specs + status, events, pod log tail."""
        aid = a["attempt_id"]
        ext = self.trial_path(aid) / "external"
        ext.mkdir(parents=True, exist_ok=True)
        pod = pod or self._pod_for(aid)
        job = self.kube.get_json("job", a["job_name"], check=False)
        _write_json(ext / "job.json", job)
        _write_json(ext / "pod.json", pod)
        try:
            ev = self.kube.get_json("events") or {}
            name = (pod or {}).get("metadata", {}).get("name")
            _write_json(ext / "events.json", [e for e in ev.get("items", []) if e.get("involvedObject", {}).get("name") in (name, a["job_name"])])
            if name:
                (ext / "pod.log").write_text(self.kube.logs(name, tail=500))
        except KubeError as e:
            _write_json(ext / "collect_error.json", {"error": str(e)})
        return pod

    def collect(self, a) -> None:
        aid = a["attempt_id"]
        pod = self.collect_external(a, None)
        tdir = self.trial_path(aid)
        result = _read_json(tdir / "result.json") if (tdir / "COMPLETE").exists() else None
        phase, job = self._job_phase(a["job_name"])
        reason = None
        if job:
            for c in job.get("status", {}).get("conditions", []):
                if c.get("type") == "Failed":
                    reason = c.get("reason")
        self.kube.delete("job", a["job_name"])     # no lingering pods: a trial pod only lives while the worker runs
        needs_verify = (result is not None and result.get("complete") and not result.get("worker_error")
                        and result.get("cgroup", {}).get("valid") and not result.get("timed_out") and not result.get("cancelled"))
        if not needs_verify:
            self._finalize(a, result, None, pod, reason)
            return
        sc = result.get("submission_copy") or {}
        if not sc.get("safe", False):
            v = {"passed": False, "checks": [{"name": "unsafe_submission", "passed": False,
                                              "detail": json.dumps(sc.get("rejected", []))[:500]}], "errors": []}
            _write_json(tdir / "verify" / "verdict.json", v)
            self._finalize(a, result, v, pod, reason)
            return
        self.state.set_state(aid, VERIFYING, verifier_job=job_name_for(aid, "v"))

    def check_verifier(self, a) -> None:
        aid = a["attempt_id"]
        vname = a["verifier_job"] or job_name_for(aid, "v")
        tdir = self.trial_path(aid)
        verdict = _read_json(tdir / "verify" / "verdict.json")
        phase, _ = self._job_phase(vname)
        if verdict is None and phase == "missing":
            if self.state.meta(f"verifier_intent:{aid}"):
                result = _read_json(tdir / "result.json")
                pod = _read_json(tdir / "external" / "pod.json")
                self._finalize(a, result, None, pod, "verifier disappeared or creation was interrupted")
                return
            others = [x for x in self.state.live_attempts() if x["state"] == VERIFYING and x["attempt_id"] != aid and x["verifier_job"]
                      and self._job_phase(x["verifier_job"])[0] in ("active",)]
            if others:
                return                           # exactly one verifier pod at a time
            (tdir / "verify").mkdir(parents=True, exist_ok=True)
            self.state.set_meta(f"verifier_intent:{aid}", str(self.now()))
            created = self.kube.create(M.verifier_job(self.cfg, self.run_id, aid, vname, a["task"]))
            self.state.event("verifier_created" if created else "verifier_adopted", vname, aid)
            return
        if verdict is None and phase == "active":
            return
        # finished (verdict present) or failed/succeeded without a verdict
        pod_v = None
        try:
            pods = self.kube.pods_for(f"{M.LABEL}/attempt={aid},{M.LABEL}/role=verifier")
            pod_v = pods[-1] if pods else None
            _write_json(tdir / "external" / "verifier_pod.json", pod_v)
        except KubeError:
            pass
        self.kube.delete("job", vname)
        result = _read_json(tdir / "result.json")
        pod = _read_json(tdir / "external" / "pod.json")
        if verdict is None and pod_oom_killed(pod_v):
            verdict = {"passed": False, "checks": [], "errors": ["verifier pod OOMKilled"]}
        self._finalize(a, result, verdict, pod, None)

    def _finalize(self, a, result, verdict, pod, reason) -> None:
        aid = a["attempt_id"]
        outcome, detail = classify(result, verdict, pod, reason)
        _write_json(self.trial_path(aid) / "outcome.json", {
            "attempt_id": aid, "slot_id": a["slot_id"], "outcome": outcome, "detail": detail, "stage": self.stage,
            "scored": self.stage == "scored", "verification": verdict, "classified_at": self.now(),
            "config_sha256": self.cfg.sha256(), "task": a["task"], "harness": a["harness"], "profile_mib": a["profile_mib"],
            "rep": a["rep"], "seq": a["seq"]})
        self.state.finish(aid, outcome, detail)

    # ------------------------------------------------------------------ cancel / finish
    def cancel_all(self) -> None:
        live = self.state.live_attempts()
        for a in live:
            self.collect_external(self.state.attempt(a["attempt_id"]), None)
            self.kube.delete("job", a["job_name"])
            if a["verifier_job"]:
                self.kube.delete("job", a["verifier_job"])
            self.state.finish(a["attempt_id"], "cancelled", "cancelled by operator")
        n = self.state.cancel_pending()
        if live or n:
            self.state.event("cancelled", f"live={len(live)} pending={n}")

    def finish_run(self) -> str:
        self.reap_orphans()
        self.state.set_meta("finished_at", str(self.now()))
        from .qualification import record_qualification
        record_qualification(self.cfg, self.state, self.run_dir, self.stage)
        (self.run_dir / "DONE").write_text(str(self.now()))
        return "done"

    def reap_orphans(self) -> None:
        """Delete child Jobs that no live attempt owns (e.g. left behind by a crash between steps)."""
        live_jobs = {a["job_name"] for a in self.state.live_attempts()} | {a["verifier_job"] for a in self.state.live_attempts() if a["verifier_job"]}
        try:
            jobs = self.kube.get_json("jobs", selector=self.selector) or {}
        except KubeError:
            return
        for j in jobs.get("items", []):
            n = j["metadata"]["name"]
            role = j["metadata"].get("labels", {}).get(f"{M.LABEL}/role")
            if role in ("trial", "verifier") and n not in live_jobs:
                self.kube.delete("job", n)
                self.state.event("orphan_deleted", n)


def main() -> int:
    cfg = config_from_frozen(Path("/etc/bench/config.json").read_text())
    kube = Kube("in-cluster", os.environ["BENCH_NAMESPACE"])
    c = Controller(cfg, kube, Path(os.environ.get("BENCH_DATA", "/data")), os.environ["BENCH_RUN_ID"],
                   os.environ.get("BENCH_STAGE", "scored"))
    return c.run_forever()


if __name__ == "__main__":
    sys.exit(main())
