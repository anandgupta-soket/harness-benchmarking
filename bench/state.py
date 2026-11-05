"""Durable controller state (SQLite, WAL) with transactional dispatch.

Invariants
- A slot has at most one *live* attempt. ``begin_attempt`` checks and inserts inside one
  BEGIN IMMEDIATE transaction, so a crash/restart can never create a duplicate attempt.
- Attempts are never deleted or overwritten. A replacement is an explicit new attempt
  created by ``replace_slot`` (with a reason) after the previous attempt is terminal.
- Job names are derived from the attempt UUID, so re-creating a Job after a crash is
  idempotent (AlreadyExists is adopted, not duplicated).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .matrix import Slot

OUTCOMES = ("passed", "incorrect", "oom", "timeout", "harness_error",
            "provider_error", "infra_error", "cancelled")
# attempt lifecycle
DISPATCHING, RUNNING, COLLECTING, VERIFYING, DONE = "dispatching", "running", "collecting", "verifying", "done"
LIVE_STATES = (DISPATCHING, RUNNING, COLLECTING, VERIFYING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slots(
  slot_id TEXT PRIMARY KEY, rep INT, task TEXT, profile_mib INT, harness TEXT,
  cell INT, ord INT,
  status TEXT NOT NULL DEFAULT 'pending'   -- pending | active | settled | cancelled
);
CREATE INDEX IF NOT EXISTS slots_cell ON slots(cell, ord);
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES slots(slot_id),
  seq INT NOT NULL, state TEXT NOT NULL, outcome TEXT, detail TEXT,
  job_name TEXT NOT NULL, verifier_job TEXT,
  created_at REAL, updated_at REAL, finished_at REAL,
  replaces TEXT, replace_reason TEXT, stage TEXT NOT NULL DEFAULT 'scored',
  UNIQUE(slot_id, seq)
);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, attempt_id TEXT, kind TEXT, msg TEXT);
"""


class StateError(Exception):
    pass


class State:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = str(path)
        if not readonly:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self.path}?mode=ro" if readonly else f"file:{self.path}"
        self.db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ---- meta
    def meta(self, k: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def event(self, kind: str, msg: str = "", attempt_id: str | None = None) -> None:
        self.db.execute("INSERT INTO events(ts,attempt_id,kind,msg) VALUES(?,?,?,?)",
                        (time.time(), attempt_id, kind, msg))

    # ---- init
    def init_run(self, run_id: str, config_sha: str, slots: list[Slot]) -> bool:
        """Idempotent. Returns True if newly created. Refuses a different config."""
        with self.tx() as db:
            existing = self.meta("config_sha")
            if existing is not None:
                if existing != config_sha:
                    raise StateError(f"state was created with config {existing[:12]}, got {config_sha[:12]}; "
                                     "frozen config is immutable")
                return False
            self.set_meta("config_sha", config_sha)
            self.set_meta("run_id", run_id)
            self.set_meta("created_at", str(time.time()))
            db.executemany(
                "INSERT INTO slots(slot_id,rep,task,profile_mib,harness,cell,ord) VALUES(?,?,?,?,?,?,?)",
                [(s.slot_id, s.rep, s.task, s.profile_mib, s.harness, s.cell, s.order) for s in slots])
            self.event("init", f"{len(slots)} slots")
        return True

    # ---- dispatch
    def live_attempts(self) -> list[sqlite3.Row]:
        q = ",".join("?" * len(LIVE_STATES))
        return self.db.execute(f"SELECT a.*, s.task, s.harness, s.profile_mib, s.rep FROM attempts a "
                               f"JOIN slots s USING(slot_id) WHERE a.state IN ({q})", LIVE_STATES).fetchall()

    def current_cell(self) -> int | None:
        r = self.db.execute("SELECT MIN(cell) c FROM slots WHERE status NOT IN ('settled','cancelled')").fetchone()
        return r["c"]

    def dispatchable(self, max_concurrent: int) -> list[sqlite3.Row]:
        """Slots that may start now: barrier = only the lowest unsettled cell; capacity =
        max_concurrent minus live attempts."""
        cell = self.current_cell()
        if cell is None:
            return []
        # capacity = trial pods in flight; attempts waiting on/inside the (single) verifier hold no trial pod
        q = ",".join("?" * 3)
        live = self.db.execute(f"SELECT COUNT(*) n FROM attempts WHERE state IN ({q})",
                               (DISPATCHING, RUNNING, COLLECTING)).fetchone()["n"]
        room = max_concurrent - live
        if room <= 0:
            return []
        return self.db.execute("SELECT * FROM slots WHERE cell=? AND status='pending' ORDER BY ord LIMIT ?",
                               (cell, room)).fetchall()

    def begin_attempt(self, slot_id: str, stage: str = "scored", replaces: str | None = None,
                      reason: str | None = None) -> sqlite3.Row:
        with self.tx() as db:
            s = db.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if s is None:
                raise StateError(f"unknown slot {slot_id}")
            if s["status"] != "pending":
                raise StateError(f"slot {slot_id} is {s['status']}, not dispatchable")
            q = ",".join("?" * len(LIVE_STATES))
            if db.execute(f"SELECT 1 FROM attempts WHERE slot_id=? AND state IN ({q})",
                          (slot_id, *LIVE_STATES)).fetchone():
                raise StateError(f"slot {slot_id} already has a live attempt")
            seq = (db.execute("SELECT COALESCE(MAX(seq),0)+1 n FROM attempts WHERE slot_id=?", (slot_id,)).fetchone()["n"])
            if seq > 1 and replaces is None:
                previous = db.execute("SELECT attempt_id FROM attempts WHERE slot_id=? ORDER BY seq DESC LIMIT 1", (slot_id,)).fetchone()
                replaces = previous["attempt_id"]
                request = db.execute("SELECT msg FROM events WHERE attempt_id=? AND kind='replace_requested' ORDER BY id DESC LIMIT 1", (replaces,)).fetchone()
                reason = request["msg"] if request else "explicit replacement"
            aid = str(uuid.uuid4())
            now = time.time()
            db.execute("INSERT INTO attempts(attempt_id,slot_id,seq,state,job_name,created_at,updated_at,replaces,replace_reason,stage) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (aid, slot_id, seq, DISPATCHING, job_name_for(aid, "t"), now, now, replaces, reason, stage))
            db.execute("UPDATE slots SET status='active' WHERE slot_id=?", (slot_id,))
            self.event("dispatch", slot_id, aid)
        return self.attempt(aid)

    def attempt(self, aid: str) -> sqlite3.Row:
        r = self.db.execute("SELECT a.*, s.task, s.harness, s.profile_mib, s.rep, s.cell FROM attempts a "
                            "JOIN slots s USING(slot_id) WHERE attempt_id=?", (aid,)).fetchone()
        if r is None:
            raise StateError(f"unknown attempt {aid}")
        return r

    def set_state(self, aid: str, state: str, **cols: Any) -> None:
        sets = ["state=?", "updated_at=?"]
        vals: list[Any] = [state, time.time()]
        for k, v in cols.items():
            if k not in ("verifier_job", "detail"):
                raise StateError(f"bad column {k}")
            sets.append(f"{k}=?")
            vals.append(v)
        self.db.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE attempt_id=? AND state!='done'", (*vals, aid))

    def finish(self, aid: str, outcome: str, detail: str = "") -> None:
        if outcome not in OUTCOMES:
            raise StateError(f"bad outcome {outcome}")
        with self.tx() as db:
            a = db.execute("SELECT slot_id,state FROM attempts WHERE attempt_id=?", (aid,)).fetchone()
            if a is None or a["state"] == DONE:
                return  # idempotent: first terminal outcome wins
            now = time.time()
            db.execute("UPDATE attempts SET state='done', outcome=?, detail=?, finished_at=?, updated_at=? WHERE attempt_id=?",
                       (outcome, detail[:2000], now, now, aid))
            db.execute("UPDATE slots SET status='settled' WHERE slot_id=?", (a["slot_id"],))
            self.event("finish", f"{outcome} {detail[:200]}", aid)

    def replace_slot(self, slot_id: str, reason: str) -> None:
        """Explicit replacement: only after the latest attempt is terminal and not 'passed'."""
        if not reason.strip():
            raise StateError("replacement requires a reason")
        with self.tx() as db:
            a = db.execute("SELECT attempt_id,state,outcome FROM attempts WHERE slot_id=? ORDER BY seq DESC LIMIT 1",
                           (slot_id,)).fetchone()
            if a is None or a["state"] != DONE:
                raise StateError("slot has no finished attempt to replace")
            if a["outcome"] == "passed":
                raise StateError("refusing to replace a passed attempt")
            db.execute("UPDATE slots SET status='pending' WHERE slot_id=?", (slot_id,))
            db.execute("INSERT INTO events(ts,attempt_id,kind,msg) VALUES(?,?,?,?)",
                       (time.time(), a["attempt_id"], "replace_requested", reason))

    def cancel_pending(self) -> int:
        with self.tx() as db:
            n = db.execute("UPDATE slots SET status='cancelled' WHERE status='pending'").rowcount
            self.event("cancel_pending", str(n))
        return n

    # ---- reporting
    def counts(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        d["slots"] = {r["status"]: r["n"] for r in
                      self.db.execute("SELECT status, COUNT(*) n FROM slots GROUP BY status")}
        d["attempts_by_state"] = {r["state"]: r["n"] for r in
                                  self.db.execute("SELECT state, COUNT(*) n FROM attempts GROUP BY state")}
        d["outcomes"] = {r["outcome"]: r["n"] for r in
                         self.db.execute("SELECT outcome, COUNT(*) n FROM attempts WHERE outcome IS NOT NULL GROUP BY outcome")}
        d["total_slots"] = sum(d["slots"].values())
        d["current_cell"] = self.current_cell()
        return d

    def all_attempts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT a.*, s.task, s.harness, s.profile_mib, s.rep, s.cell FROM attempts a JOIN slots s USING(slot_id) "
            "ORDER BY s.cell, s.ord, a.seq")]

    def to_json(self) -> str:
        return json.dumps(self.counts(), sort_keys=True)


def job_name_for(attempt_id: str, kind: str) -> str:
    """Deterministic, DNS-1123-safe Job name from the attempt UUID."""
    return f"bench-{kind}-{attempt_id.replace('-', '')[:20]}"
