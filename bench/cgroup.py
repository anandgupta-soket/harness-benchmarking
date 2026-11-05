"""Container-scoped cgroup counters (v1 authoritative, v2 supported). Missing metric -> None.

Detection never infers v2 from a ``0::/`` line in /proc/self/cgroup (hybrid hosts show
both): v2 is used only when /sys/fs/cgroup is itself a cgroup2 mount that has
cgroup.controllers. Otherwise v1 is used iff the v1 memory counters exist. Anything else
fails qualification (``detect().ok`` is False) instead of silently guessing.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except (OSError, ValueError):
        return None


def _int(p: Path) -> int | None:
    v = _read(p)
    if v is None or v == "max":
        return None
    try:
        return int(v.split()[0])
    except ValueError:
        return None


def _kv(p: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    txt = _read(p)
    for line in (txt or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


class Cgroup:
    def __init__(self, root: str | Path = "/sys/fs/cgroup", mountinfo: str | Path = "/proc/self/mountinfo"):
        self.root = Path(root)
        self.mountinfo = Path(mountinfo)
        self.info = self.detect()

    # ------------------------------------------------------------------ detection
    def _root_is_cgroup2(self) -> bool:
        txt = _read(self.mountinfo) or ""
        for line in txt.splitlines():
            parts = line.split(" - ")
            if len(parts) != 2:
                continue
            pre, post = parts
            fields = pre.split()
            if len(fields) > 4 and fields[4] == str(self.root) and post.split()[0] == "cgroup2":
                return True
        return False

    def detect(self) -> dict[str, Any]:
        reasons: list[str] = []
        if (self.root / "cgroup.controllers").exists() and self._root_is_cgroup2():
            ctrl = (_read(self.root / "cgroup.controllers") or "").split()
            for need in ("memory", "cpu"):
                if need not in ctrl and not (self.root / ("memory.current" if need == "memory" else "cpu.stat")).exists():
                    reasons.append(f"cgroup v2 controller {need} not available")
            return {"version": "v2", "memory_dir": str(self.root), "cpu_dir": str(self.root), "ok": not reasons, "reasons": reasons}
        mem = self.root / "memory"
        if (mem / "memory.max_usage_in_bytes").exists():
            cpu = next((self.root / d for d in ("cpu,cpuacct", "cpuacct,cpu", "cpu") if (self.root / d / "cpu.stat").exists()), None)
            acct = next((self.root / d for d in ("cpu,cpuacct", "cpuacct,cpu", "cpuacct") if (self.root / d / "cpuacct.usage").exists()), None)
            if cpu is None:
                reasons.append("cgroup v1: cpu controller dir with cpu.stat not found")
            if acct is None:
                reasons.append("cgroup v1: cpuacct.usage not found")
            return {"version": "v1", "memory_dir": str(mem), "cpu_dir": str(cpu) if cpu else None,
                    "cpuacct_dir": str(acct) if acct else None, "ok": not reasons, "reasons": reasons,
                    "hybrid_unified_mount_present": (self.root / "unified").exists()}
        return {"version": None, "ok": False,
                "reasons": ["no usable cgroup hierarchy: neither a cgroup2 root mount with controllers nor v1 memory.max_usage_in_bytes"]}

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        v = self.info.get("version")
        s: dict[str, Any] = {k: None for k in (
            "mem_usage", "mem_peak", "mem_limit", "mem_rss", "mem_cache", "mem_inactive_file", "mem_working_set",
            "oom_kill", "mem_failcnt", "cpu_usage_ns", "cpu_nr_periods", "cpu_nr_throttled", "cpu_throttled_ns",
            "cpu_quota_us", "cpu_period_us")}
        s["cgroup_version"] = v
        if v == "v1":
            m = Path(self.info["memory_dir"])
            s["mem_usage"] = _int(m / "memory.usage_in_bytes")
            s["mem_peak"] = _int(m / "memory.max_usage_in_bytes")
            lim = _int(m / "memory.limit_in_bytes")
            s["mem_limit"] = lim if lim is not None and lim < (1 << 60) else None
            s["mem_failcnt"] = _int(m / "memory.failcnt")
            st = _kv(m / "memory.stat")
            s["mem_rss"] = st.get("total_rss", st.get("rss"))
            s["mem_cache"] = st.get("total_cache", st.get("cache"))
            s["mem_inactive_file"] = st.get("total_inactive_file", st.get("inactive_file"))
            oc = _kv(m / "memory.oom_control")
            s["oom_kill"] = oc.get("oom_kill")  # absent on older kernels -> None (pod status is the fallback)
            if self.info.get("cpuacct_dir"):
                ns = _int(Path(self.info["cpuacct_dir"]) / "cpuacct.usage")
                s["cpu_usage_ns"] = ns
            if self.info.get("cpu_dir"):
                c = Path(self.info["cpu_dir"])
                cs = _kv(c / "cpu.stat")
                s["cpu_nr_periods"] = cs.get("nr_periods")
                s["cpu_nr_throttled"] = cs.get("nr_throttled")
                s["cpu_throttled_ns"] = cs.get("throttled_time")
                q = _int(c / "cpu.cfs_quota_us")
                s["cpu_quota_us"] = q if q is not None and q > 0 else None
                s["cpu_period_us"] = _int(c / "cpu.cfs_period_us")
        elif v == "v2":
            r = Path(self.info["memory_dir"])
            s["mem_usage"] = _int(r / "memory.current")
            s["mem_peak"] = _int(r / "memory.peak")   # kernel >= 5.19 only; else None
            s["mem_limit"] = _int(r / "memory.max")
            st = _kv(r / "memory.stat")
            s["mem_rss"] = st.get("anon")
            s["mem_cache"] = st.get("file")
            s["mem_inactive_file"] = st.get("inactive_file")
            ev = _kv(r / "memory.events")
            s["oom_kill"] = ev.get("oom_kill")
            cs = _kv(r / "cpu.stat")
            us = cs.get("usage_usec")
            s["cpu_usage_ns"] = us * 1000 if us is not None else None
            s["cpu_nr_periods"] = cs.get("nr_periods")
            s["cpu_nr_throttled"] = cs.get("nr_throttled")
            tu = cs.get("throttled_usec")
            s["cpu_throttled_ns"] = tu * 1000 if tu is not None else None
            mx = (_read(r / "cpu.max") or "").split()
            if len(mx) == 2 and mx[0] != "max":
                s["cpu_quota_us"], s["cpu_period_us"] = int(mx[0]), int(mx[1])
        if s["mem_usage"] is not None and s["mem_inactive_file"] is not None:
            s["mem_working_set"] = max(0, s["mem_usage"] - s["mem_inactive_file"])
        return s

    # ------------------------------------------------------------------ qualification
    def validate(self, expected_mem_bytes: int, expected_cpu: float = 1.0) -> dict[str, Any]:
        """Is the visible hierarchy container-scoped and carrying the limits we asked for?"""
        reasons = list(self.info.get("reasons", []))
        s = self.snapshot() if self.info.get("version") else {}
        if self.info.get("version"):
            if s["mem_limit"] != expected_mem_bytes:
                reasons.append(f"memory limit {s['mem_limit']} != expected {expected_mem_bytes}: hierarchy is not the container's own")
            if s["mem_usage"] is None:
                reasons.append("memory usage counter unreadable")
            if s["mem_peak"] is None and self.info["version"] == "v1":
                reasons.append("memory.max_usage_in_bytes unreadable")
            if s["cpu_usage_ns"] is None:
                reasons.append("cpu usage counter unreadable")
            if s["cpu_quota_us"] is None or not s["cpu_period_us"]:
                reasons.append("cpu quota/period not visible")
            elif abs(s["cpu_quota_us"] / s["cpu_period_us"] - expected_cpu) > 1e-6:
                reasons.append(f"cpu quota {s['cpu_quota_us']}/{s['cpu_period_us']} != {expected_cpu} cores")
            if s["cpu_nr_throttled"] is None:
                reasons.append("cpu throttling counters unreadable")
        return {"ok": not reasons, "reasons": reasons, "detect": self.info, "snapshot": s,
                "notes": ["mem_peak is the kernel peak and includes page cache (reclaimable); mem_rss/mem_working_set are sampled",
                          "v2 peak requires memory.peak (kernel>=5.19); otherwise null"]}


class Sampler(threading.Thread):
    """Polls counters; keeps peak sampled RSS/working-set and writes JSONL samples."""

    def __init__(self, cg: Cgroup, out_path: str | Path | None, interval: float = 1.0):
        super().__init__(daemon=True)
        self.cg, self.interval = cg, interval
        self.out = open(out_path, "a", buffering=1) if out_path else None
        self.stop_evt = threading.Event()
        self.peak_rss: int | None = None
        self.peak_ws: int | None = None
        self.peak_usage: int | None = None
        self.n = 0

    def run(self) -> None:
        import json
        while not self.stop_evt.is_set():
            self.sample_once(json)
            self.stop_evt.wait(self.interval)

    def sample_once(self, json=None) -> dict:
        import json as _j
        s = self.cg.snapshot()
        s["t"] = time.time()
        for key, attr in (("mem_rss", "peak_rss"), ("mem_working_set", "peak_ws"), ("mem_usage", "peak_usage")):
            v = s.get(key)
            if v is not None and (getattr(self, attr) is None or v > getattr(self, attr)):
                setattr(self, attr, v)
        self.n += 1
        if self.out:
            self.out.write(_j.dumps(s) + "\n")
        return s

    def stop(self) -> None:
        self.stop_evt.set()
        self.join(timeout=5)
        if self.out:
            self.out.close()
