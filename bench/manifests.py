"""Kubernetes manifest rendering (pure functions -> dicts). No secrets are ever embedded:
the API key is only referenced through ``secretKeyRef`` and only in trial/probe pods.

Pod budget (worst case, enforced by ``audit_budget`` and a ResourceQuota):
controller + max 4 trial pods + 1 verifier (+1 reader/probe) <= 8 pods,
<=16 CPU, <=32Gi memory, <=40Gi ephemeral-storage. Every container sets
requests == limits for cpu, memory and ephemeral-storage.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import Config, K8S_NAME_RE

LABEL = "bench.soket.ai"
DATA_MOUNT = "/data"
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
AGENT_UID = 10001


def _safe(v: str, what: str) -> str:
    if not SAFE_ID_RE.match(v):
        raise ValueError(f"unsafe {what}: {v!r}")
    return v


def _res(cpu: float | int | str, mem_mi: int, eph_gi: float) -> dict[str, dict[str, str]]:
    r = {"cpu": str(cpu), "memory": f"{int(mem_mi)}Mi", "ephemeral-storage": f"{int(eph_gi * 1024)}Mi"}
    return {"requests": dict(r), "limits": dict(r)}


def meta(cfg: Config, name: str, role: str, run_id: str | None = None, attempt: str | None = None) -> dict[str, Any]:
    labels = {"app.kubernetes.io/name": "loop-bench", f"{LABEL}/role": role}
    if run_id:
        labels[f"{LABEL}/run"] = _safe(run_id, "run id")
    if attempt:
        labels[f"{LABEL}/attempt"] = _safe(attempt, "attempt id")
    return {"name": name, "namespace": cfg.namespace, "labels": labels}


def trial_dir(run_id: str, attempt_id: str) -> str:
    return f"runs/{_safe(run_id, 'run')}/trials/{_safe(attempt_id, 'attempt')}"


# --------------------------------------------------------------------------- setup objects
def pvc(cfg: Config) -> dict:
    s = cfg.data["storage"]
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": s["pvc_name"], "namespace": cfg.namespace,
                         "labels": {"app.kubernetes.io/name": "loop-bench"}},
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": s["storage_class"],
                     "resources": {"requests": {"storage": s["size"]}}}}


def service_account(cfg: Config) -> dict:
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(cfg, "bench-controller", "rbac")}


def role(cfg: Config) -> dict:
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta(cfg, "bench-controller", "rbac"),
            "rules": [
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch", "delete", "patch"]},
                {"apiGroups": [""], "resources": ["pods", "events"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["delete"]},
            ]}


def role_binding(cfg: Config) -> dict:
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(cfg, "bench-controller", "rbac"),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "bench-controller"},
            "subjects": [{"kind": "ServiceAccount", "name": "bench-controller", "namespace": cfg.namespace}]}


def resource_quota(cfg: Config) -> dict:
    b = cfg.data["budget"]
    return {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": meta(cfg, "bench-budget", "quota"),
            "spec": {"hard": {"pods": str(b["max_pods"]),
                              "requests.cpu": str(b["max_cpu"]), "limits.cpu": str(b["max_cpu"]),
                              "requests.memory": f"{b['max_memory_gi']}Gi", "limits.memory": f"{b['max_memory_gi']}Gi",
                              "requests.ephemeral-storage": f"{b['max_ephemeral_gi']}Gi",
                              "limits.ephemeral-storage": f"{b['max_ephemeral_gi']}Gi"}}}


def network_policy_trial(cfg: Config) -> dict:
    """Optional (--network-policy): trial/verifier pods may not reach in-cluster pods or
    the kube API; DNS + egress to the public internet stay open. Requires a CNI that
    enforces NetworkPolicy; on local-path/flannel-only clusters it is a no-op."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(cfg, "bench-trial-egress", "netpol"),
            "spec": {"podSelector": {"matchExpressions": [{"key": f"{LABEL}/role", "operator": "In", "values": ["trial", "verifier"]}]},
                     "policyTypes": ["Ingress", "Egress"], "ingress": [],
                     "egress": [{"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
                                {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}]}]}}


def setup_objects(cfg: Config, network_policy: bool = False) -> list[dict]:
    objs = [pvc(cfg), service_account(cfg), role(cfg), role_binding(cfg), resource_quota(cfg)]
    if network_policy:
        objs.append(network_policy_trial(cfg))
    return objs


# --------------------------------------------------------------------------- pods
def _pod_common(cfg: Config, role_name: str) -> dict[str, Any]:
    return {
        "restartPolicy": "Never" if role_name != "controller" else "OnFailure",
        "automountServiceAccountToken": role_name == "controller",
        "enableServiceLinks": False,
        "nodeSelector": {"kubernetes.io/hostname": cfg.get("kube.worker_node")},
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
    }


def _config_volume() -> dict:
    return {"name": "bench-config", "configMap": {"name": "PLACEHOLDER"}}


def _job(cfg: Config, name: str, role_name: str, run_id: str, attempt: str | None, pod_spec: dict,
         deadline: int | None, backoff: int = 0) -> dict:
    m = meta(cfg, name, role_name, run_id, attempt)
    spec: dict[str, Any] = {"backoffLimit": backoff, "completions": 1, "parallelism": 1,
                            "template": {"metadata": {"labels": dict(m["labels"])}, "spec": pod_spec}}
    if deadline is not None:
        spec["activeDeadlineSeconds"] = int(deadline)
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": m, "spec": spec}


def config_map_name(run_id: str) -> str:
    return f"bench-config-{run_id}"[:63].rstrip("-")


def control_map_name(run_id: str) -> str:
    return f"bench-control-{run_id}"[:63].rstrip("-")


def config_map(cfg: Config, run_id: str) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(cfg, config_map_name(run_id), "config", run_id),
            "immutable": True,
            "data": {"config.json": cfg.frozen_json(), "config.sha256": cfg.sha256()}}


def control_map(cfg: Config, run_id: str, state: str = "running") -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(cfg, control_map_name(run_id), "control", run_id),
            "data": {"state": state}}


def _cfg_vol(run_id: str) -> dict:
    return {"name": "bench-config", "configMap": {"name": config_map_name(run_id)}}


def controller_job(cfg: Config, run_id: str, stage: str = "scored") -> dict:
    img = cfg.get("images.runner")
    c = cfg.data["budget"]["controller"]
    container = {
        "name": "controller", "image": img, "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "bench.controller"],
        "env": [{"name": "BENCH_RUN_ID", "value": run_id}, {"name": "BENCH_STAGE", "value": stage},
                {"name": "BENCH_NAMESPACE", "value": cfg.namespace}, {"name": "BENCH_ACCESS", "value": "in-cluster"},
                {"name": "BENCH_DATA", "value": DATA_MOUNT}],
        "resources": _res(c["cpu"], c["memory_mi"], c["ephemeral_gi"]),
        "volumeMounts": [{"name": "data", "mountPath": DATA_MOUNT}, {"name": "bench-config", "mountPath": "/etc/bench", "readOnly": True}],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    spec = {**_pod_common(cfg, "controller"), "serviceAccountName": "bench-controller", "containers": [container],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": cfg.get("storage.pvc_name")}}, _cfg_vol(run_id)]}
    # controller restarts (OnFailure) are the crash-recovery mechanism; state is reconciled from SQLite + the API.
    return _job(cfg, f"bench-controller-{run_id}"[:63].rstrip("-"), "controller", run_id, None, spec, deadline=None, backoff=50)


def trial_job(cfg: Config, run_id: str, attempt_id: str, job_name: str, task_id: str, harness: str,
              profile_mib: int, timeout_s: int, stage: str = "scored") -> dict:
    _safe(task_id, "task"), _safe(harness, "harness")
    sec = cfg.data["model"]["api_key_secret"]
    env = [
        {"name": "BENCH_RUN_ID", "value": run_id}, {"name": "BENCH_ATTEMPT_ID", "value": attempt_id},
        {"name": "BENCH_TASK", "value": task_id}, {"name": "BENCH_HARNESS", "value": harness},
        {"name": "BENCH_PROFILE_MIB", "value": str(int(profile_mib))}, {"name": "BENCH_TIMEOUT_S", "value": str(int(timeout_s))},
        {"name": "BENCH_STAGE", "value": stage},
        {"name": cfg.data["model"]["api_key_env"],
         "valueFrom": {"secretKeyRef": {"name": sec["name"], "key": sec["key"]}}},
    ]
    t = cfg.data["budget"]["trial"]
    container = {
        "name": "trial", "image": cfg.get("images.runner"), "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "bench.worker", "trial"],
        "env": env,
        "resources": _res(cfg.data["experiment"]["cpu"], profile_mib, t["ephemeral_gi"]),
        "volumeMounts": [
            {"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"},
            {"name": "data", "mountPath": "/out", "subPath": trial_dir(run_id, attempt_id)},
            {"name": "bench-config", "mountPath": "/etc/bench", "readOnly": True}],
        # root worker supervises; the agent is dropped to AGENT_UID. Only the caps needed for that.
        "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"], "add": ["SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL"]}},
    }
    spec = {**_pod_common(cfg, "trial"), "containers": [container], "terminationGracePeriodSeconds": 30,
            "volumes": [{"name": "work", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                        {"name": "data", "persistentVolumeClaim": {"claimName": cfg.get("storage.pvc_name")}}, _cfg_vol(run_id)]}
    slack = int(cfg.data["timeouts"]["grace_seconds"]) + 600  # startup + safe copy + artifact flush
    return _job(cfg, job_name, "trial", run_id, attempt_id, spec, deadline=timeout_s + slack)


def verifier_job(cfg: Config, run_id: str, attempt_id: str, job_name: str, task_id: str) -> dict:
    _safe(task_id, "task")
    v = cfg.data["budget"]["verifier"]
    td = trial_dir(run_id, attempt_id)
    container = {
        "name": "verifier", "image": cfg.get("images.runner"), "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "bench.worker", "verify"],
        "env": [{"name": "BENCH_TASK", "value": task_id}, {"name": "BENCH_ATTEMPT_ID", "value": attempt_id}],
        "resources": _res(v["cpu"], v["memory_mi"], v["ephemeral_gi"]),
        "volumeMounts": [
            {"name": "data", "mountPath": "/trial", "subPath": td, "readOnly": True},
            {"name": "data", "mountPath": "/verify-out", "subPath": td + "/verify"},
            {"name": "scratch", "mountPath": "/scratch"}, {"name": "tmp", "mountPath": "/tmp"}],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    spec = {**_pod_common(cfg, "verifier"), "containers": [container], "terminationGracePeriodSeconds": 10,
            "volumes": [{"name": "scratch", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                        {"name": "data", "persistentVolumeClaim": {"claimName": cfg.get("storage.pvc_name")}}]}
    # no Secret, no config, no service-account token: the verifier never sees credentials.
    return _job(cfg, job_name, "verifier", run_id, attempt_id, spec, deadline=int(cfg.data["timeouts"]["verifier_seconds"]) + 120)


def reader_pod(cfg: Config, name: str = "bench-reader", ttl_s: int = 3600) -> dict:
    r = cfg.data["budget"]["reader"]
    container = {
        "name": "reader", "image": cfg.get("images.runner"), "imagePullPolicy": "IfNotPresent",
        "command": ["sleep", str(int(ttl_s))],
        "resources": _res(r["cpu"], r["memory_mi"], r["ephemeral_gi"]),
        # The operator helper persists qualification evidence and explicit
        # replacement state in SQLite; do not mount this PVC read-only.
        "volumeMounts": [{"name": "data", "mountPath": DATA_MOUNT}],
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    spec = {**_pod_common(cfg, "reader"), "containers": [container], "activeDeadlineSeconds": int(ttl_s) + 30,
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": cfg.get("storage.pvc_name")}}]}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta(cfg, name, "reader"), "spec": spec}


def probe_job(cfg: Config, name: str, profile_mib: int, probe_id: str) -> dict:
    """cgroup-qualification probe: same limits as a trial, no Secret, no PVC."""
    _safe(probe_id, "probe id")
    t = cfg.data["budget"]["trial"]
    container = {
        "name": "probe", "image": cfg.get("images.runner"), "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "bench.worker", "probe"],
        "env": [{"name": "BENCH_PROFILE_MIB", "value": str(int(profile_mib))}],
        "resources": _res(cfg.data["experiment"]["cpu"], profile_mib, t["ephemeral_gi"]),
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    spec = {**_pod_common(cfg, "probe"), "containers": [container]}
    return _job(cfg, name, "probe", probe_id, None, spec, deadline=300)


# --------------------------------------------------------------------------- budget audit
def to_cores(v: str) -> float:
    v = str(v)
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def to_gi(v: str) -> float:
    m = re.match(r"^([0-9.]+)(Ki|Mi|Gi)$", str(v))
    if not m:
        raise ValueError(f"unparseable quantity {v}")
    return float(m.group(1)) / {"Ki": 1024 ** 2, "Mi": 1024, "Gi": 1}[m.group(2)]


def pod_spec_of(obj: dict) -> dict:
    return obj["spec"]["template"]["spec"] if obj["kind"] == "Job" else obj["spec"]


def audit_containers(obj: dict) -> list[str]:
    errs = []
    for c in pod_spec_of(obj)["containers"]:
        r = c.get("resources") or {}
        for k in ("requests", "limits"):
            for res in ("cpu", "memory", "ephemeral-storage"):
                if not (r.get(k) or {}).get(res):
                    errs.append(f"{obj['metadata']['name']}/{c['name']}: missing {k}.{res}")
        if r.get("requests") != r.get("limits"):
            errs.append(f"{obj['metadata']['name']}/{c['name']}: requests != limits")
    return errs


def pod_totals(obj: dict) -> tuple[float, float, float]:
    """(cpu cores, memory Gi, ephemeral Gi) of a Job/Pod object (limits)."""
    cpu = mem = eph = 0.0
    for c in pod_spec_of(obj)["containers"]:
        lim = c["resources"]["limits"]
        cpu += to_cores(lim["cpu"])
        mem += to_gi(lim["memory"])
        eph += to_gi(lim["ephemeral-storage"])
    return cpu, mem, eph


def audit_budget(cfg: Config) -> dict[str, Any]:
    """Render the worst-case concurrent pod set and check every cap. Raises on violation."""
    run = "audit"
    mx = cfg.data["experiment"]["max_concurrent_trials"]
    worst_profile = max(cfg.profiles)
    objs = [controller_job(cfg, run)]
    objs += [trial_job(cfg, run, f"a{i}", f"t{i}", "BUG-01", "loop", worst_profile, 60) for i in range(mx)]
    objs += [verifier_job(cfg, run, "a0", "v0", "BUG-01"), reader_pod(cfg)]
    errs: list[str] = []
    for o in objs:
        errs += audit_containers(o)
    tot = [pod_totals(o) for o in objs]
    cpu, mem, eph = (sum(t[i] for t in tot) for i in range(3))
    b = cfg.data["budget"]
    pods = len(objs)
    if pods > b["max_pods"]:
        errs.append(f"pods {pods} > {b['max_pods']}")
    if cpu > b["max_cpu"]:
        errs.append(f"cpu {cpu} > {b['max_cpu']}")
    if mem > b["max_memory_gi"]:
        errs.append(f"memory {mem:.2f}Gi > {b['max_memory_gi']}Gi")
    if eph > b["max_ephemeral_gi"]:
        errs.append(f"ephemeral {eph:.2f}Gi > {b['max_ephemeral_gi']}Gi")
    if errs:
        raise ValueError("budget audit failed:\n  " + "\n  ".join(errs))
    return {"pods": pods, "cpu": cpu, "memory_gi": round(mem, 3), "ephemeral_gi": round(eph, 3)}


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)
