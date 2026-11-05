"""Experiment configuration: loading, validation, placeholder detection, freezing.

Required values (endpoint, model, Secret ref, image, harness versions, Loop commit)
must be concrete before anything that touches the cluster (setup/run/qualify) is
allowed to proceed. ``plan`` tolerates unresolved fields but reports them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HARNESSES = ("loop", "pi", "goose", "opencode", "hermes", "cline", "kilo", "omp", "deepseek-harness")
PLACEHOLDER_RE = re.compile(r"^\s*(UNRESOLVED|TBD|TODO|CHANGEME|REPLACE_ME|<[^>]*>|\.\.\.)", re.I)
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_STR_RE = re.compile(r"^[A-Za-z0-9._:/@+=-]+$")
ALLOWED_INTERNAL_HTTP_ENDPOINT = "http://172.17.99.145:30000/v1"


class ConfigError(Exception):
    pass


def is_placeholder(v: Any) -> bool:
    return v is None or (isinstance(v, str) and (v.strip() == "" or bool(PLACEHOLDER_RE.match(v))))


def _get(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def required_concrete(harnesses: list[str]) -> list[str]:
    return [
        "model.base_url", "model.name", "model.provider_id",
        "model.api_key_secret.name", "model.api_key_secret.key",
        "images.runner", "loop.repo", "loop.commit",
        "kube.namespace", "kube.worker_node",
    ] + [f"harnesses.{h}.version" for h in harnesses]


DEFAULTS: dict[str, Any] = {
    "experiment": {
        "name": "unnamed", "seed": 0, "repetitions": 5,
        "memory_mib": [256, 512, 1024, 2048], "cpu": 1,
        "max_concurrent_trials": 4,
        "reference_memory_mib": 2048,
    },
    "kube": {
        "access": "ssh", "ssh_host": "kube.dev.soket.ai", "namespace": "bench-constrained",
        "context": None, "worker_node": None,
    },
    "storage": {"pvc_name": "bench-data", "size": "20Gi", "storage_class": "local-path",
                "min_free_fraction": 0.20},
    "budget": {
        "max_pods": 8, "max_cpu": 16, "max_memory_gi": 32, "max_ephemeral_gi": 40,
        "controller": {"cpu": 0.5, "memory_mi": 1024, "ephemeral_gi": 2},
        "trial": {"ephemeral_gi": 6},
        "verifier": {"cpu": 2, "memory_mi": 4096, "ephemeral_gi": 8},
        "reader": {"cpu": 0.25, "memory_mi": 256, "ephemeral_gi": 1},
    },
    "model": {"api_key_env": "BENCH_API_KEY"},
    "limits_disclosure": {"token_ceiling": "not-enforced", "turn_ceiling": "not-enforced"},
    "qualification": {
        "protocol": {"task": "BUG-01", "profile_mib": 2048},
        "matrix": {"harnesses": ["loop", "pi", "goose", "opencode"], "tasks": ["BUG-01", "FEAT-02", "WEB-BUG-01"],
                   "profiles": [512, 2048], "repetitions": 1},
    },
    "timeouts": {"snapshot_seconds": 60, "verifier_seconds": 900, "grace_seconds": 60, "heartbeat_seconds": 15},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    data: dict[str, Any]
    path: Path | None = None
    problems: list[str] = field(default_factory=list)   # structural errors
    unresolved: list[str] = field(default_factory=list)  # placeholder fields

    def get(self, path: str, default: Any = None) -> Any:
        v = _get(self.data, path)
        return default if v is None else v

    @property
    def namespace(self) -> str:
        return self.data["kube"]["namespace"]

    @property
    def harnesses(self) -> list[str]:
        return list((self.data.get("matrix") or {}).get("harnesses") or HARNESSES)

    @property
    def profiles(self) -> list[int]:
        return [int(x) for x in self.data["experiment"]["memory_mib"]]

    def frozen_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.frozen_json().encode()).hexdigest()

    def require_digest_pinned(self) -> None:
        """Scored launches need an immutable image: tags are mutable, digests are not."""
        img = self.get("images.runner", "")
        if not DIGEST_RE.search(str(img)):
            raise ConfigError(f"images.runner must be digest-pinned (…@sha256:<64 hex>) for a scored run; got {img!r}. "
                              "Resolve it with `bench image resolve` and put the digest in the config.")

    def require_resolved(self) -> None:
        if self.problems or self.unresolved:
            lines = [f"config {self.path or ''} is not runnable (fail closed):"]
            lines += [f"  invalid: {p}" for p in self.problems]
            lines += [f"  unresolved: {u}" for u in self.unresolved]
            raise ConfigError("\n".join(lines))


def validate(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    harnesses = list((data.get("matrix") or {}).get("harnesses") or HARNESSES)
    unresolved = [p for p in required_concrete(harnesses) if is_placeholder(_get(data, p))]
    if len(harnesses) != len(set(harnesses)):
        problems.append("matrix.harnesses must not contain duplicates")
    for name in ("api_key", "apiKey", "key", "token", "password", "authorization"):
        if name in (data.get("model") or {}):
            problems.append(f"model.{name} must not contain credentials; use a Secret reference")
    ns = _get(data, "kube.namespace")
    if isinstance(ns, str) and not is_placeholder(ns) and not K8S_NAME_RE.match(ns):
        problems.append(f"kube.namespace {ns!r} is not a valid k8s name")
    access = _get(data, "kube.access")
    if access not in ("ssh", "local-kubectl"):
        problems.append("kube.access must be 'ssh' or 'local-kubectl'")
    host = _get(data, "kube.ssh_host")
    if access == "ssh" and (not isinstance(host, str) or not SAFE_STR_RE.match(host)):
        problems.append("kube.ssh_host missing/unsafe")
    for p in ("model.base_url", "model.name", "model.provider_id", "model.api_key_secret.name",
              "model.api_key_secret.key", "loop.repo", "kube.worker_node"):
        v = _get(data, p)
        if isinstance(v, str) and not is_placeholder(v) and not SAFE_STR_RE.match(v):
            problems.append(f"{p} contains unsafe characters")
    ep = _get(data, "model.base_url")
    if (isinstance(ep, str) and not is_placeholder(ep)
            and not (ep.startswith("https://") or ep == ALLOWED_INTERNAL_HTTP_ENDPOINT)):
        problems.append("model.base_url must use https:// (or the explicitly allowlisted internal HTTP endpoint)")
    if isinstance(ep, str) and not is_placeholder(ep):
        from urllib.parse import urlsplit
        if urlsplit(ep).username or urlsplit(ep).password:
            problems.append("model.base_url must not embed credentials")
    sec = _get(data, "model.api_key_secret.name")
    if isinstance(sec, str) and not is_placeholder(sec) and not K8S_NAME_RE.match(sec):
        problems.append("model.api_key_secret.name is not a valid k8s name")
    img = _get(data, "images.runner")
    if isinstance(img, str) and not is_placeholder(img):
        last = img.rsplit("/", 1)[-1]
        if img.endswith(":latest") or (":" not in last and "@" not in img):
            problems.append("images.runner must be an explicit tag or digest, never 'latest'/untagged")
        if not SAFE_STR_RE.match(img):
            problems.append("images.runner contains unsafe characters")
    commit = _get(data, "loop.commit")
    if isinstance(commit, str) and not is_placeholder(commit) and not SHA_RE.match(commit):
        problems.append("loop.commit must be a full 40-hex SHA")
    for h in harnesses:
        if h not in HARNESSES:
            problems.append(f"unknown harness {h!r}")
        v = _get(data, f"harnesses.{h}.version")
        if isinstance(v, str) and not is_placeholder(v):
            if v.lower() in ("latest", "main", "master", "head") or not SAFE_STR_RE.match(v):
                problems.append(f"harnesses.{h}.version must be an exact pinned version, got {v!r}")
    provider = _get(data, "model.provider_id")
    if isinstance(provider, str) and not is_placeholder(provider) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", provider):
        problems.append("model.provider_id must be a simple provider identifier, not a path")
    secret_key = _get(data, "model.api_key_secret.key")
    if isinstance(secret_key, str) and not is_placeholder(secret_key) and not re.fullmatch(r"[A-Za-z0-9._-]+", secret_key):
        problems.append("model.api_key_secret.key is invalid")
    ake = _get(data, "model.api_key_env")
    if not isinstance(ake, str) or not re.match(r"^[A-Z][A-Z0-9_]{2,63}$", ake):
        problems.append("model.api_key_env must be an env var NAME like BENCH_API_KEY (never the key itself)")
    ex = data.get("experiment", {})
    if ex.get("cpu") != 1:
        problems.append("experiment.cpu must be 1 (v1 matrix)")
    mc = ex.get("max_concurrent_trials", 0)
    if not isinstance(mc, int) or not 1 <= mc <= 4:
        problems.append("experiment.max_concurrent_trials must be 1..4")
    mems = ex.get("memory_mib") or []
    if not mems or any((not isinstance(m, int)) or m <= 0 for m in mems):
        problems.append("experiment.memory_mib must be positive ints")
    elif len(mems) != len(set(mems)):
        problems.append("experiment.memory_mib must not contain duplicates")
    if not isinstance(ex.get("repetitions"), int) or ex["repetitions"] < 1:
        problems.append("experiment.repetitions must be >= 1")
    mf = _get(data, "storage.min_free_fraction")
    if not isinstance(mf, (int, float)) or not 0 < mf < 1:
        problems.append("storage.min_free_fraction must be in (0,1)")
    pvc = _get(data, "storage.pvc_name")
    if not isinstance(pvc, str) or not K8S_NAME_RE.match(pvc):
        problems.append("storage.pvc_name invalid")
    for k in ("token_ceiling", "turn_ceiling"):
        if _get(data, f"limits_disclosure.{k}") not in ("not-enforced", "enforced-by-harness"):
            problems.append(f"limits_disclosure.{k} must be 'not-enforced' or 'enforced-by-harness'")
    sh = contains_secret_like(data)
    if sh:
        problems.append(f"secret-like values in config at {sh}")
    return problems, unresolved


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read config {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    data = deep_merge(DEFAULTS, raw)
    problems, unresolved = validate(data)
    return Config(data=data, path=p, problems=problems, unresolved=unresolved)


def config_from_frozen(text: str) -> Config:
    data = json.loads(text)
    problems, unresolved = validate(data)
    return Config(data=data, problems=problems, unresolved=unresolved)


_SECRET_PAT = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|eyJ[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._-]{16,})")


def contains_secret_like(obj: Any) -> list[str]:
    """Heuristic scan used before any config is written to the cluster."""
    hits: list[str] = []

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and _SECRET_PAT.search(o):
            hits.append(path)

    walk(obj, "")
    return hits
