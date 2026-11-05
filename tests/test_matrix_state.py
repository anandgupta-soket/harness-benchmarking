import pytest

from bench.config import HARNESSES
from bench.matrix import build_matrix, waves
from bench.state import State, StateError
from bench.tasks import TASK_IDS, load_tasks

PROFILES = [256, 512, 1024, 2048]


def test_full_matrix_is_2340_and_deterministic():
    a = build_matrix(list(TASK_IDS), list(HARNESSES), PROFILES, 5, seed=7)
    b = build_matrix(list(TASK_IDS), list(HARNESSES), PROFILES, 5, seed=7)
    c = build_matrix(list(TASK_IDS), list(HARNESSES), PROFILES, 5, seed=8)
    assert len(a) == 13 * 9 * 4 * 5 == 2340
    assert a == b and a != c
    assert len({s.slot_id for s in a}) == 2340


def test_cells_contain_all_harnesses_and_reps_are_blocks():
    m = build_matrix(list(TASK_IDS), list(HARNESSES), PROFILES, 5, seed=1)
    cells = {}
    for s in m:
        cells.setdefault(s.cell, []).append(s)
    assert len(cells) == 13 * 4 * 5
    for cs in cells.values():
        assert sorted(x.harness for x in cs) == sorted(HARNESSES)
        assert len({(x.rep, x.task, x.profile_mib) for x in cs}) == 1
    reps = [cells[c][0].rep for c in sorted(cells)]
    assert reps == sorted(reps)
    assert [len(w) for w in waves(cells[0], 4)] == [4, 4, 1]


def test_tasks_load():
    ts = load_tasks()
    assert [t.id for t in ts] == list(TASK_IDS)
    assert all(t.timeout_minutes > 0 for t in ts)


@pytest.fixture
def st(tmp_path):
    s = State(tmp_path / "s.db")
    slots = build_matrix(["BUG-01", "FEAT-01"], ["loop", "pi", "goose"], [256], 1, seed=3)
    s.init_run("r", "cfg1", slots)
    return s


def test_barrier_and_concurrency(st):
    d = st.dispatchable(2)
    assert len(d) == 2 and {r["cell"] for r in d} == {0}
    a1 = st.begin_attempt(d[0]["slot_id"])
    a2 = st.begin_attempt(d[1]["slot_id"])
    assert st.dispatchable(2) == []           # capacity full
    st.finish(a1["attempt_id"], "passed")
    r = st.dispatchable(2)                     # third of cell 0 only, never cell 1
    assert [x["cell"] for x in r] == [0]
    a3 = st.begin_attempt(r[0]["slot_id"])
    st.finish(a2["attempt_id"], "incorrect")
    assert st.dispatchable(4) == []            # barrier: a3 still live
    st.finish(a3["attempt_id"], "oom")
    assert {x["cell"] for x in st.dispatchable(4)} == {1}


def test_no_duplicate_attempt_and_idempotent_init(st):
    s = st.dispatchable(1)[0]["slot_id"]
    st.begin_attempt(s)
    with pytest.raises(StateError):
        st.begin_attempt(s)
    assert st.init_run("r", "cfg1", []) is False
    with pytest.raises(StateError):
        st.init_run("r", "other", [])


def test_restart_reopens_same_state(tmp_path):
    p = tmp_path / "x.db"
    s = State(p)
    s.init_run("r", "c", build_matrix(["BUG-01"], ["loop"], [256], 1, 0))
    a = s.begin_attempt("r1.BUG-01.256.loop")
    s2 = State(p)
    assert [r["attempt_id"] for r in s2.live_attempts()] == [a["attempt_id"]]
    assert s2.dispatchable(4) == []


def test_replacement_is_explicit_and_retains_history(st):
    sid = st.dispatchable(1)[0]["slot_id"]
    a = st.begin_attempt(sid)
    st.finish(a["attempt_id"], "infra_error", "node lost")
    st.finish(a["attempt_id"], "passed")        # first terminal outcome wins
    assert st.attempt(a["attempt_id"])["outcome"] == "infra_error"
    with pytest.raises(StateError):
        st.replace_slot(sid, "")
    st.replace_slot(sid, "node drained")
    b = st.begin_attempt(sid, replaces=a["attempt_id"], reason="node drained")
    assert b["seq"] == 2 and b["attempt_id"] != a["attempt_id"]
    assert len([x for x in st.all_attempts() if x["slot_id"] == sid]) == 2
    st.finish(b["attempt_id"], "passed")
    with pytest.raises(StateError):
        st.replace_slot(sid, "again")           # cannot replace a pass


def test_storage_uses_quota_not_just_statvfs():
    from bench.storage import evaluate, parse_size
    G = 1024 ** 3
    # node disk is huge and nearly empty, but the data dir already holds 17Gi of the 20Gi quota
    s = evaluate(parse_size("20Gi"), 17 * G, 500 * G, 400 * G, 0.20)
    assert s.free == 3 * G and not s.ok
    # quota fine but the underlying filesystem is almost full
    s = evaluate(parse_size("20Gi"), 1 * G, 30 * G, 2 * G, 0.20)
    assert s.free == 2 * G and not s.ok
    assert evaluate(parse_size("20Gi"), 1 * G, 500 * G, 400 * G, 0.20).ok


def test_storage_dir_usage(tmp_path):
    from bench.storage import dir_usage
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 10000)
    (tmp_path / "ln").symlink_to(tmp_path / "a")
    assert dir_usage(tmp_path) >= 10000
