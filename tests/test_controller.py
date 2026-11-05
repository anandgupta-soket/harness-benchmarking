import json
from pathlib import Path

import pytest

from bench import controller as C
from bench import storage as S
from bench.config import DEFAULTS, Config, deep_merge, validate
from bench.kube import Kube, KubeError, ReadOnlyViolation
from bench.state import State

from .fakekube import FakeKube, World


def make_cfg(harnesses=("loop", "pi", "goose", "opencode", "hermes"), mems=(256,), reps=1):
    d = deep_merge(DEFAULTS, {
        "experiment": {"seed": 1, "repetitions": reps, "memory_mib": list(mems)},
        "kube": {"worker_node": "w1"}, "matrix": {"harnesses": list(harnesses)},
        "model": {"base_url": "https://x.invalid/v1", "name": "m", "provider_id": "p", "api_key_secret": {"name": "s", "key": "k"}},
        "images": {"runner": "r.invalid/x:1"}, "loop": {"repo": "a/b", "commit": "0" * 40},
        "harnesses": {h: {"version": "1.0.0"} for h in harnesses}})
    p, u = validate(d)
    return Config(d, None, p, u)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "check", lambda *a, **k: S.evaluate(10, 0, 100, 100, 0.2))
    cfg = make_cfg()
    kube = FakeKube()
    c = C.Controller(cfg, kube, tmp_path, "r1", "scored")
    c.init()
    return cfg, kube, c, World(kube, tmp_path, "r1"), tmp_path


def drive(c, w, steps=400):
    for _ in range(steps):
        r = c.step()
        w.tick()
        if r == "done":
            return r
    return r


def test_full_run_completes_with_barrier_and_pod_caps(env):
    cfg, kube, c, w, _ = env
    assert drive(c, w) == "done"
    cnt = c.state.counts()
    assert cnt["total_slots"] == 13 * 5 and cnt["outcomes"] == {"passed": 65}
    assert kube.max_live_trials <= 4 and kube.max_live_verifiers <= 1
    atts = c.state.all_attempts()
    assert len(atts) == 65 and len({a["attempt_id"] for a in atts}) == 65
    assert not [k for k in kube.objs if k[0] == "job"], "no lingering Jobs after completion"
    assert (c.run_dir / "DONE").exists()
    o = json.loads((c.trial_path(atts[0]["attempt_id"]) / "outcome.json").read_text())
    assert o["outcome"] == "passed" and o["scored"] is True


def test_barrier_never_mixes_cells(env):
    cfg, kube, c, w, _ = env
    seen = []
    for _ in range(60):
        c.step()
        live_cells = {c.state.attempt(a["attempt_id"])["cell"] for a in c.state.live_attempts()}
        assert len(live_cells) <= 1
        seen.append(live_cells)
        w.tick(finish=1)


def test_restart_reconciles_without_duplicates(env, tmp_path):
    cfg, kube, c, w, data = env
    for _ in range(3):
        c.step()
    created_before = list(kube.created_log)
    attempts_before = len(c.state.all_attempts())
    # crash/restart: brand-new controller object over the same DB and the same cluster
    c2 = C.Controller(cfg, kube, data, "r1", "scored")
    c2.init()
    c2.step()
    assert len(c2.state.all_attempts()) >= attempts_before
    assert [n for n in kube.created_log if kube.created_log.count(n) > 1] == []
    assert set(created_before) <= set(kube.created_log)
    assert drive(c2, w) == "done"
    assert len(c2.state.all_attempts()) == 65


def test_dispatching_crash_adopts_existing_job(env):
    cfg, kube, c, w, data = env
    slot = c.state.dispatchable(4)[0]
    a = c.state.begin_attempt(slot["slot_id"])          # committed in DB, crash before/after Job create
    from bench import manifests as M
    kube.create(M.trial_job(cfg, "r1", a["attempt_id"], a["job_name"], a["task"], a["harness"], a["profile_mib"], 60))
    c2 = C.Controller(cfg, kube, data, "r1", "scored")
    c2.advance(c2.state.attempt(a["attempt_id"]))
    assert kube.created_log.count(a["job_name"]) == 1
    assert c2.state.attempt(a["attempt_id"])["state"] == "running"


def test_pause_drains_then_resume(env):
    cfg, kube, c, w, _ = env
    c.step()
    live_before = len(c.state.live_attempts())
    assert live_before > 0
    kube.control = "paused"
    n_att = len(c.state.all_attempts())
    assert c.step() == "paused"
    w.tick()
    for _ in range(10):
        c.step()
        w.tick()
    assert len(c.state.all_attempts()) == n_att, "paused controller must not dispatch"
    assert not c.state.live_attempts(), "running attempts drained"
    kube.control = "running"
    assert drive(c, w) == "done"


def test_cancel_deletes_children_and_keeps_history(env):
    cfg, kube, c, w, _ = env
    for _ in range(3):
        c.step()
    kube.control = "cancelled"
    assert c.step() == "cancelled"
    assert not [k for k in kube.objs if k[0] == "job"]
    cnt = c.state.counts()
    assert cnt["slots"].get("pending", 0) == 0 and cnt["outcomes"].get("cancelled", 0) >= 1
    assert len(c.state.all_attempts()) >= 1


def test_storage_gate_blocks_dispatch(env, monkeypatch):
    cfg, kube, c, w, _ = env
    monkeypatch.setattr(S, "check", lambda *a, **k: S.evaluate(100, 90, 1000, 1000, 0.2))
    assert c.step() == "blocked" and not c.state.all_attempts()
    assert c.state.meta("dispatch_blocked") == "storage"


def test_external_oom_timeout_and_infra(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "check", lambda *a, **k: S.evaluate(10, 0, 100, 100, 0.2))
    order = {}

    def scenario(info):
        if info["role"] != "trial":
            return {}
        n = order.setdefault(info["attempt"], len(order))
        return [{"oom_pod": True}, {"die_no_result": True}, {"timed_out": True}, {"unsafe": True}, {"passed": False}][n % 5]
    kube = FakeKube()
    c = C.Controller(make_cfg(), kube, tmp_path, "r2", "scored")
    c.init()
    w = World(kube, tmp_path, "r2", scenario)
    drive(c, w)
    outs = {}
    for a in c.state.all_attempts():
        outs.setdefault(a["outcome"], 0)
        outs[a["outcome"]] += 1
    assert outs["oom"] >= 1 and outs["infra_error"] >= 1 and outs["timeout"] >= 1 and outs["incorrect"] >= 1
    assert len(c.state.all_attempts()) == 65, "failed attempts are never auto-replaced"
    unsafe = [a for a in c.state.all_attempts() if a["outcome"] == "incorrect"]
    assert unsafe


def test_unsafe_submission_skips_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "check", lambda *a, **k: S.evaluate(10, 0, 100, 100, 0.2))
    kube = FakeKube()
    c = C.Controller(make_cfg(("loop",)), kube, tmp_path, "r3", "scored")
    c.init()
    w = World(kube, tmp_path, "r3", lambda i: {"unsafe": True})
    drive(c, w)
    assert not [n for n in kube.created_log if n.startswith("bench-v-")]
    assert c.state.counts()["outcomes"] == {"incorrect": 13}


def test_verifier_missing_verdict_is_infra_error(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "check", lambda *a, **k: S.evaluate(10, 0, 100, 100, 0.2))
    kube = FakeKube()
    c = C.Controller(make_cfg(("loop",)), kube, tmp_path, "r4", "scored")
    c.init()

    class W(World):
        def tick(self, finish=99):
            super().tick(finish)
            for (k, n), j in kube.objs.items():          # verifier pod dies without writing a verdict
                if k == "job" and j["metadata"]["labels"]["bench.soket.ai/role"] == "verifier" and not j["status"]:
                    j["status"] = {"failed": 1}
            return 0
    drive(c, W(kube, tmp_path, "r4", lambda info: {"verifier_hang": True} if info["role"] == "verifier" else {}), 300)
    assert c.state.counts()["outcomes"] == {"infra_error": 13}


def test_plan_style_readonly_kube_refuses_mutation():
    calls = []
    k = Kube("local-kubectl", "bench-constrained", read_only=True,
             runner=lambda argv, stdin, t: calls.append(argv) or __import__("subprocess").CompletedProcess(argv, 0, b"{}", b""))
    k.get_json("pods")
    for verb in ("apply", "create", "delete", "patch"):
        with pytest.raises(ReadOnlyViolation):
            k.run([verb, "x"])
    assert len(calls) == 1 and "get" in calls[0]


def test_ssh_argv_is_quoted_not_interpolated():
    k = Kube("ssh", "bench-constrained", ssh_host="kube.dev.soket.ai")
    argv = k.full_argv(["get", "pods", "-l", "a=b; rm -rf /"])
    assert argv[0] == "ssh" and argv[-2] == "kube.dev.soket.ai"
    assert argv[-1] == "kubectl -n bench-constrained get pods -l 'a=b; rm -rf /'"
    with pytest.raises(KubeError):
        k.kubectl_argv(["get", "x\ny"])
