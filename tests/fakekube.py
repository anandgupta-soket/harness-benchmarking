"""In-memory stand-in for the kubectl-backed Kube used by controller tests, plus a 'world' that plays
the role of worker/verifier pods by writing the same files the real ones write."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from bench.kube import KubeError


def sel_match(labels: dict, selector: str | None) -> bool:
    if not selector:
        return True
    return all(labels.get(k) == v for k, v in (p.split("=", 1) for p in selector.split(",")))


class FakeKube:
    def __init__(self):
        self.objs: dict[tuple[str, str], dict] = {}
        self.created_log: list[str] = []
        self.deleted_log: list[str] = []
        self.control = "running"
        self.max_live_trials = 0
        self.max_live_verifiers = 0

    # ---- Kube API subset
    def create(self, obj: dict) -> bool:
        key = (obj["kind"].lower(), obj["metadata"]["name"])
        if key in self.objs:
            return False
        o = copy.deepcopy(obj)
        o["status"] = {}
        self.objs[key] = o
        self.created_log.append(obj["metadata"]["name"])
        live = [j for (k, _), j in self.objs.items() if k == "job" and not j["status"]]
        self.max_live_trials = max(self.max_live_trials, sum(1 for j in live if j["metadata"]["labels"]["bench.soket.ai/role"] == "trial"))
        self.max_live_verifiers = max(self.max_live_verifiers, sum(1 for j in live if j["metadata"]["labels"]["bench.soket.ai/role"] == "verifier"))
        return True

    def get_json(self, kind, name=None, selector=None, check=True):
        kind1 = kind.rstrip("s") if kind.endswith("s") and kind != "events" else kind
        if kind == "configmap":
            return {"data": {"state": self.control}}
        if kind == "events":
            return {"items": []}
        if name:
            o = self.objs.get((kind1, name))
            if o is None:
                if check:
                    raise KubeError("NotFound")
                return None
            return copy.deepcopy(o)
        return {"items": [copy.deepcopy(o) for (k, _), o in self.objs.items()
                          if k == kind1 and sel_match(o["metadata"].get("labels", {}), selector)]}

    def pods_for(self, selector):
        out = []
        for (k, n), o in self.objs.items():
            if k == "job" and sel_match(o["metadata"]["labels"], selector) and o.get("pod"):
                out.append(o["pod"])
        return out

    def delete(self, kind, name=None, selector=None, background=True):
        for key in [k for k, o in self.objs.items() if k[0] == kind and (name is None or k[1] == name)
                    and sel_match(o["metadata"].get("labels", {}), selector)]:
            self.deleted_log.append(key[1])
            del self.objs[key]

    def logs(self, pod, container=None, tail=None):
        return "log"


class World:
    """Simulates worker + verifier behaviour. scenario(job_labels) -> dict of behaviour."""

    def __init__(self, kube: FakeKube, data: Path, run_id: str, scenario=None):
        self.kube, self.data, self.run_id = kube, Path(data), run_id
        self.scenario = scenario or (lambda a: {})

    def tdir(self, aid):
        return self.data / "runs" / self.run_id / "trials" / aid

    def tick(self, finish: int = 99) -> int:
        n = 0
        for (k, name), job in list(self.kube.objs.items()):
            if k != "job" or job["status"] or n >= finish:
                continue
            lab = job["metadata"]["labels"]
            aid, role = lab["bench.soket.ai/attempt"], lab["bench.soket.ai/role"]
            sc = self.scenario({"attempt": aid, "role": role, "job": job})
            td = self.tdir(aid)
            if role == "trial":
                self._finish_trial(job, td, sc)
            elif role == "verifier":
                if sc.get("verifier_hang"):
                    continue
                (td / "verify").mkdir(parents=True, exist_ok=True)
                (td / "verify" / "verdict.json").write_text(json.dumps(
                    {"task_id": "x", "passed": sc.get("passed", True), "checks": [{"name": "c", "passed": sc.get("passed", True)}], "errors": []}))
                job["status"] = {"succeeded": 1}
                job["pod"] = {"metadata": {"name": f"{name}-p"}, "status": {"phase": "Succeeded"}}
            n += 1
        return n

    def _finish_trial(self, job, td, sc):
        env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        if sc.get("hang"):
            return
        if sc.get("oom_pod"):
            job["status"] = {"failed": 1}
            job["pod"] = {"metadata": {"name": "p"}, "status": {"phase": "Failed", "containerStatuses": [
                {"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
            return
        if sc.get("die_no_result"):
            job["status"] = {"failed": 1}
            job["pod"] = {"metadata": {"name": "p"}, "status": {"phase": "Failed", "reason": "Evicted"}}
            return
        td.mkdir(parents=True, exist_ok=True)
        (td / "submission").mkdir(exist_ok=True)
        (td / "submission" / "f").write_text("x")
        res = {"complete": True, "cgroup": {"valid": True, "oom_kill_delta": 0}, "agent": {"exit_code": 0}, "timed_out": sc.get("timed_out", False),
               "timeout_s": 1, "usage": {"total_tokens": 5}, "submission_copy": {"safe": not sc.get("unsafe"), "rejected": [{"path": "l", "reason": "symlink"}] if sc.get("unsafe") else []}}
        (td / "result.json").write_text(json.dumps(res))
        (td / "COMPLETE").write_text("1")
        job["status"] = {"succeeded": 1}
        job["pod"] = {"metadata": {"name": "p"}, "status": {"phase": "Succeeded"}}
