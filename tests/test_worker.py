"""Worker tests with a fake harness process (a python script standing in for the agent)."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from bench import harnesses as H
from bench.config import DEFAULTS, deep_merge
from bench.outcome import classify
from bench.worker import Redactor, TrialParams, run_trial

MIB = 1024 * 1024
SECRET = "sk-TESTSECRET-0123456789abcdef"


class FakeCg:
    """Stands in for the cgroup: oom_kill increments if the script touches OOM_FLAG."""
    def __init__(self, oom_flag: Path | None = None, valid=True):
        self.info = {"version": "v1", "ok": valid, "reasons": [] if valid else ["bad"]}
        self.oom_flag, self.valid, self.t = oom_flag, valid, 0

    def snapshot(self):
        self.t += 1
        oom = 3 if (self.oom_flag and self.oom_flag.exists()) else 0
        return {"mem_usage": 10 * MIB, "mem_peak": 50 * MIB + self.t, "mem_limit": 128 * MIB, "mem_rss": 20 * MIB,
                "mem_cache": 1, "mem_inactive_file": 1, "mem_working_set": 12 * MIB, "oom_kill": oom, "mem_failcnt": 0,
                "cpu_usage_ns": self.t * 10**9, "cpu_nr_periods": 5, "cpu_nr_throttled": 1, "cpu_throttled_ns": 1000,
                "cpu_quota_us": 100000, "cpu_period_us": 100000, "cgroup_version": "v1"}

    def validate(self, expected_mem_bytes, expected_cpu=1.0):
        return {"ok": self.valid, "reasons": [] if self.valid else ["cgroup invalid"], "detect": self.info, "snapshot": self.snapshot()}


def make_fixture(root: Path) -> str:
    fx = root / "fixtures" / "fx"
    (fx / "src").mkdir(parents=True, exist_ok=True)
    (fx / "src" / "lib.rs").write_text("old")
    return "fixtures/fx"


def params(tmp_path, **kw):
    fixrel = make_fixture(tmp_path)
    cfg = deep_merge(DEFAULTS, {"model": {"base_url": "https://x.invalid/v1", "name": "m", "provider_id": "p",
                                          "api_key_secret": {"name": "s", "key": "k"}}})
    base = dict(run_id="r", attempt_id="a1", task_id="BUG-01", harness="loop", profile_mib=128, timeout_s=20,
                out=tmp_path / "out", work=tmp_path / "work", fixtures=tmp_path / "fixtures", fixture_rel=fixrel,
                prompt="p", cfg=cfg, agent_uid=None, snapshot_seconds=0.3, sample_seconds=0.1)
    base.update(kw)
    return TrialParams(**base)


def fake_build(script: str, scrape=None):
    def build(harness, ctx):
        f = ctx.home.parent / "fake_agent.py"
        f.write_text(textwrap.dedent(script))
        return H.Invocation([sys.executable, str(f)], {"PATH": os.environ["PATH"], "HOME": str(ctx.home),
                                                       "BENCH_API_KEY": os.environ.get("BENCH_API_KEY", ""),
                                                       "OOM_FLAG": os.environ.get("OOM_FLAG", "")},
                            scrape or (lambda o, e: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                                     "assistant_turns": 2, "tool_calls": 1}))
    return build


GOOD = """
import os, pathlib
pathlib.Path('src/lib.rs').write_text('new')
print('working with key', os.environ.get('BENCH_API_KEY'))
"""


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("BENCH_API_KEY", SECRET)


def read(tmp_path, name):
    return (tmp_path / "out" / name).read_text()


def test_success_artifacts_and_no_secret_leak(tmp_path):
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(GOOD))
    out = tmp_path / "out"
    assert r["complete"] and (out / "COMPLETE").exists() and not r.get("worker_error")
    assert r["agent"]["exit_code"] == 0 and not r["timed_out"]
    assert (out / "submission/src/lib.rs").read_text() == "new"
    assert "-old" in (out / "diff.patch").read_text() or "old" in (out / "diff.patch").read_text()
    assert (out / "workspace-full.tar.gz").exists() and (out / "resource_samples.jsonl").stat().st_size > 0
    assert r["cgroup"]["valid"] and r["cgroup"]["peak_memory_bytes"] >= 50 * MIB and r["cgroup"]["oom_kill_delta"] == 0
    assert r["cgroup"]["cpu_seconds"] > 0 and r["usage"]["total_tokens"] == 15
    for f in out.rglob("*"):
        if f.is_file():
            assert SECRET not in f.read_bytes().decode(errors="ignore"), f
    assert "[REDACTED]" in read(tmp_path, "agent.stdout.log")
    assert classify(r, {"passed": True, "checks": []})[0] == "passed"
    assert classify(r, {"passed": False, "checks": [{"name": "x", "passed": False}]})[0] == "incorrect"


def test_crash_is_harness_error_and_provider_error_detected(tmp_path):
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build("import sys; print('Traceback boom', file=sys.stderr); sys.exit(7)"))
    assert r["agent"]["exit_code"] == 7
    assert classify(r, {"passed": False, "checks": []})[0] == "harness_error"
    r = run_trial(params(tmp_path, attempt_id="a2", out=tmp_path / "o2"), cg=FakeCg(),
                  build=fake_build("import sys; print('HTTP 502 upstream_error The model is unavailable', file=sys.stderr); sys.exit(1)"))
    assert classify(r, {"passed": False, "checks": []})[0] == "provider_error"


def test_missing_usage_is_null_not_zero(tmp_path):
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(GOOD, scrape=lambda o, e: None))
    assert r["usage"] is None and r["usage_missing"] is True
    assert classify(r, {"passed": True, "checks": []})[0] == "passed"


def test_timeout_kills_tree_including_session_escapers(tmp_path):
    marker = tmp_path / "alive"
    script = f"""
        import os, subprocess, sys, time
        # same-group child and a setsid escapee, both writing a heartbeat file
        child = "import time,pathlib\\nwhile True:\\n    pathlib.Path(r'{marker}').write_text(str(time.time())); time.sleep(0.1)"
        subprocess.Popen([sys.executable, "-c", child])
        subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
        time.sleep(300)
    """
    t0 = time.time()
    r = run_trial(params(tmp_path, timeout_s=2), cg=FakeCg(), build=fake_build(script))
    assert r["timed_out"] and time.time() - t0 < 30
    assert r["agent"]["signal"] == 9
    assert classify(r, {"passed": True, "checks": []})[0] == "timeout"    # a pass never rescues a timeout
    time.sleep(0.5)
    m1 = marker.read_text() if marker.exists() else ""
    time.sleep(0.6)
    m2 = marker.read_text() if marker.exists() else ""
    assert m1 == m2, "descendant still running after timeout"


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="env-marker sweep of reparented escapees needs Linux /proc (runs in the image)")
def test_descendants_killed_after_normal_exit(tmp_path):
    marker = tmp_path / "bg"
    script = f"""
        import subprocess, sys
        child = "import time,pathlib\\nwhile True:\\n    pathlib.Path(r'{marker}').write_text(str(time.time())); time.sleep(0.1)"
        subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
    """
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(script))
    assert r["agent"]["exit_code"] == 0
    time.sleep(0.5)
    a = marker.read_text() if marker.exists() else ""
    time.sleep(0.5)
    assert a == (marker.read_text() if marker.exists() else "")


def test_oom_from_cgroup_events(tmp_path):
    flag = tmp_path / "oomflag"
    os.environ["OOM_FLAG"] = str(flag)
    r = run_trial(params(tmp_path), cg=FakeCg(flag), build=fake_build(f"import pathlib; pathlib.Path(r'{flag}').write_text('1'); import sys; sys.exit(137)"))
    assert r["cgroup"]["oom_kill_delta"] == 3
    assert classify(r, {"passed": False, "checks": []})[0] == "oom"
    assert classify(r, {"passed": True, "checks": []})[0] == "passed"       # passed wins; flag stays in result


def test_invalid_cgroup_is_infra_error_not_fake_metrics(tmp_path):
    r = run_trial(params(tmp_path), cg=FakeCg(valid=False), build=fake_build(GOOD))
    assert classify(r, {"passed": True, "checks": []})[0] == "infra_error"


def test_external_oomkilled_pod_without_result():
    pod = {"status": {"containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
    assert classify(None, None, pod)[0] == "oom"
    assert classify({"complete": False}, None, {"status": {"reason": "Evicted"}})[0] == "infra_error"
    assert classify(None, None, None, "DeadlineExceeded")[0] == "timeout"
    assert classify(None, None, None, cancelled=True)[0] == "cancelled"


def test_unsafe_submission_recorded_and_launch_failure(tmp_path):
    script = "import os; os.symlink('/etc/passwd', 'src/evil')"
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(script))
    assert not r["submission_copy"]["safe"] and not (tmp_path / "out/submission/src/evil").exists()
    # the symlink survives only inside the forensic tar as a link, never as a followed copy
    def bad_build(h, c):
        return H.Invocation(["/nonexistent/agent"], {"PATH": ""}, lambda o, e: None)
    r2 = run_trial(params(tmp_path, attempt_id="a3", out=tmp_path / "o3"), cg=FakeCg(), build=bad_build)
    assert r2["agent"]["launch_failed"]
    assert classify(r2, {"passed": False, "checks": []})[0] == "harness_error"


def test_interrupted_collection_has_no_complete_marker(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "result.partial.json").write_text("{}")
    assert not (out / "COMPLETE").exists()
    assert classify(None, None, {"status": {"reason": "Evicted"}})[0] == "infra_error"


def test_redactor():
    r = Redactor([SECRET, "", "xy"])
    assert SECRET not in r(f"a {SECRET} b") and r("xy") == "[REDACTED]" and r("keep") == "keep"
    assert "ab\\\"c" not in Redactor(['ab"c'])('ab\\"c')     # JSON-escaped form is also redacted


def test_pump_redacts_secret_split_across_chunks_and_caps(tmp_path):
    import io
    from bench.worker import Pump
    data = b"A" * 100 + SECRET.encode() + b"B" * 100

    class Slow(io.RawIOBase):
        def __init__(self): self.b = io.BytesIO(data)
        def read1(self, n): return self.b.read(37)       # split mid-secret
    pm = Pump(Slow(), tmp_path / "l", Redactor([SECRET]))
    pm.start(), pm.join()
    out = (tmp_path / "l").read_bytes()
    assert SECRET.encode() not in out and b"[REDACTED]" in out and out.startswith(b"A" * 100)
    pm2 = Pump(io.BytesIO(b"z" * 5000), tmp_path / "l2", Redactor([]), cap=1000)
    pm2.start(), pm2.join()
    assert pm2.truncated and (tmp_path / "l2").stat().st_size < 1200


def test_sigterm_handler_restored(tmp_path):
    import signal
    before = signal.getsignal(signal.SIGTERM)
    run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(GOOD))
    assert signal.getsignal(signal.SIGTERM) == before


def test_archive_contains_no_secret_even_if_agent_wrote_it(tmp_path):
    import tarfile
    script = "import os,pathlib; pathlib.Path('src/leak.txt').write_text(os.environ['BENCH_API_KEY'])"
    run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(script))
    with tarfile.open(tmp_path / "out/workspace-full.tar.gz") as tf:
        blob = b"".join(tf.extractfile(m).read() for m in tf.getmembers() if m.isfile())
    assert SECRET.encode() not in blob and b"[REDACTED]" in blob
    assert SECRET.encode() not in (tmp_path / "out/diff.patch").read_bytes()


def test_harness_state_excludes_credential_stores(tmp_path):
    script = "import pathlib,os; h=pathlib.Path(os.environ['HOME']); (h/'auth.json').write_text('K'); (h/'sessions').mkdir(); (h/'sessions'/'s.jsonl').write_text('ok')"
    r = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(script))
    assert (tmp_path / "out/harness_state/sessions/s.jsonl").exists() and not (tmp_path / "out/harness_state/auth.json").exists()
    assert "auth.json" in r["harness_state_copy"]["skipped_files"]

