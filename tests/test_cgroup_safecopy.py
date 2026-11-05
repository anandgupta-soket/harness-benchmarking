import os

from bench.cgroup import Cgroup
from bench.safecopy import safe_copy

MIB128 = 134217728


def mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def fake_v1(root, limit=MIB128, quota=10000, hybrid_unified=True):
    mk(root, "memory/memory.usage_in_bytes", "1000\n")
    mk(root, "memory/memory.max_usage_in_bytes", "5000\n")
    mk(root, "memory/memory.limit_in_bytes", f"{limit}\n")
    mk(root, "memory/memory.failcnt", "0\n")
    mk(root, "memory/memory.stat", "total_rss 700\ntotal_cache 300\ntotal_inactive_file 100\n")
    mk(root, "memory/memory.oom_control", "oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    mk(root, "cpu,cpuacct/cpuacct.usage", "123456789\n")
    mk(root, "cpu,cpuacct/cpu.stat", "nr_periods 10\nnr_throttled 4\nthrottled_time 4000000\n")
    mk(root, "cpu,cpuacct/cpu.cfs_quota_us", f"{quota}\n")
    mk(root, "cpu,cpuacct/cpu.cfs_period_us", "100000\n")
    if hybrid_unified:
        (root / "unified").mkdir()


def test_v1_hybrid_detected_as_v1_and_read(tmp_path):
    fake_v1(tmp_path)
    mi = tmp_path / "mi"
    mi.write_text("")  # no cgroup2 mount at root; a '0::/' line elsewhere must not matter
    cg = Cgroup(tmp_path, mi)
    assert cg.info["version"] == "v1" and cg.info["ok"]
    s = cg.snapshot()
    assert s["mem_peak"] == 5000 and s["mem_limit"] == MIB128 and s["oom_kill"] == 2
    assert s["mem_rss"] == 700 and s["mem_working_set"] == 900
    assert s["cpu_usage_ns"] == 123456789 and s["cpu_nr_throttled"] == 4 and s["cpu_throttled_ns"] == 4000000
    assert cg.validate(MIB128, 0.1)["ok"]


def test_validation_fails_on_wrong_scope_and_missing(tmp_path):
    fake_v1(tmp_path, limit=1 << 62)   # host-root-like hierarchy: no container limit visible
    v = Cgroup(tmp_path, tmp_path / "none").validate(MIB128, 0.1)
    assert not v["ok"] and any("not the container's own" in r for r in v["reasons"])
    empty = tmp_path / "e"
    empty.mkdir()
    v = Cgroup(empty, empty / "x").validate(MIB128)
    assert not v["ok"] and v["detect"]["version"] is None


def test_v2(tmp_path):
    root = tmp_path / "cg"
    mk(root, "cgroup.controllers", "cpu memory\n")
    mk(root, "memory.current", "10\n")
    mk(root, "memory.max", f"{MIB128}\n")
    mk(root, "memory.stat", "anon 7\nfile 3\ninactive_file 1\n")
    mk(root, "memory.events", "oom 1\noom_kill 1\n")
    mk(root, "cpu.stat", "usage_usec 500\nnr_periods 3\nnr_throttled 1\nthrottled_usec 20\n")
    mk(root, "cpu.max", "100000 100000\n")
    mi = tmp_path / "mi"
    mi.write_text(f"1 0 0:1 / {root} rw - cgroup2 cgroup2 rw\n")
    cg = Cgroup(root, mi)
    assert cg.info["version"] == "v2"
    s = cg.snapshot()
    assert s["mem_peak"] is None            # no memory.peak -> null, not invented
    assert s["oom_kill"] == 1 and s["cpu_usage_ns"] == 500000 and s["cpu_quota_us"] == 100000
    assert not cg.validate(MIB128)["ok"] is False


def test_safe_copy_rejects_symlinks_and_escapes(tmp_path):
    src, dst, outside = tmp_path / "s", tmp_path / "d", tmp_path / "outside"
    src.mkdir(), outside.mkdir()
    (outside / "secret").write_text("S")
    (src / "ok.txt").write_text("hi")
    (src / "sub").mkdir()
    (src / "sub" / "a.rs").write_text("fn main(){}")
    os.symlink(outside / "secret", src / "link")
    os.symlink(outside, src / "sub" / "dirlink")
    os.symlink("../../etc/passwd", src / "rel")
    os.mkfifo(src / "fifo")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "x").write_text("y")
    rep = safe_copy(src, dst)
    assert not rep.safe
    reasons = {r["path"]: r["reason"] for r in rep.rejected}
    assert reasons["link"] == "symlink" and reasons["sub/dirlink"] == "symlink" and reasons["rel"] == "symlink"
    assert "special" in reasons["fifo"]
    assert (dst / "ok.txt").read_text() == "hi" and (dst / "sub" / "a.rs").exists()
    assert not (dst / "link").exists() and not (dst / "node_modules").exists()
    assert "node_modules" in rep.skipped_dirs
    assert not any("secret" in p.name for p in dst.rglob("*"))


def test_safe_copy_caps_and_clean(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    (src / "big").write_bytes(b"x" * 5000)
    (src / "small").write_text("a")
    rep = safe_copy(src, dst, max_file=1000)
    assert not rep.safe and (dst / "small").exists() and not (dst / "big").exists()
    ok = safe_copy(tmp_path / "s2" if False else src, tmp_path / "d2", max_file=10_000)
    assert ok.safe and ok.files == 2
