"""In-pod entrypoints: ``python -m bench.worker trial|verify|probe``.

trial: runs ONE harness on ONE task inside the pod's cgroup. The worker is a small
supervisor in the same container cgroup as the agent (so the memory limit is shared by
harness + all of its tools, as required); its own footprint (a few MB of Python) is
included in the measured numbers and disclosed. It:
  * copies the pristine fixture into /work/project (agent-owned),
  * runs the harness as an unprivileged uid with a scrubbed env, stdout/stderr pumped
    through a secret redactor straight to the PVC (/out, root-only, unreachable by the agent),
  * enforces the task's declared timeout by killing the whole process tree (no restarts,
    no fallback timeout),
  * reads container-scoped cgroup counters (peak memory, CPU, throttling, OOM kills),
  * periodically snapshots the workspace so a whole-pod OOM kill still leaves evidence,
  * safe-copies the final workspace (rejecting symlinks / escapes) and archives it,
  * writes result.json then a COMPLETE marker (atomic rename).
Nothing here grades correctness: a separate clean verifier pod does that.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import cgroup as cgm
from . import harnesses as H
from .paths import FIXTURES_DIR, REPO_ROOT
from .safecopy import SECRET_DIR_NAMES, SECRET_FILE_GLOBS, CopyReport, safe_copy

AGENT_UID = int(os.environ.get("BENCH_AGENT_UID", "10001"))
TAIL = 4000


# ------------------------------------------------------------------ redaction
class Redactor:
    """Replaces every non-empty credential (raw and JSON-escaped forms). No minimum length."""

    def __init__(self, secrets: list[str]):
        forms: set[str] = set()
        for sec in secrets:
            if sec:
                forms.add(sec)
                esc = json.dumps(sec)[1:-1]
                forms.add(esc)
        self.secrets = sorted(forms, key=len, reverse=True)
        self.maxlen = max((len(x.encode()) for x in self.secrets), default=0)

    def __call__(self, text: str) -> str:
        for s_ in self.secrets:
            text = text.replace(s_, "[REDACTED]")
        return text

    def bytes(self, b: bytes) -> bytes:
        for s_ in self.secrets:
            b = b.replace(s_.encode(), b"[REDACTED]")
        return b


def redact_tree(root: Path, red: Redactor, max_size: int = 64 << 20) -> int:
    n = 0
    for p in root.rglob("*"):
        try:
            if p.is_symlink() or not p.is_file() or p.stat().st_size > max_size:
                continue
            data = p.read_bytes()
            new = red.bytes(data)
            if new != data:
                p.write_bytes(new)
                n += 1
        except OSError:
            continue
    return n


LOG_CAP = int(os.environ.get("BENCH_LOG_CAP", str(32 << 20)))


class Pump(threading.Thread):
    """Drain a pipe in bounded chunks to a file through the redactor. A (maxlen-1)-byte carry is held back
    between chunks so a secret split across reads is still redacted. Output beyond LOG_CAP is dropped (the pipe
    keeps being drained so the agent never blocks) and a truncation marker is written. Memory use is O(chunk)."""

    def __init__(self, pipe, path: Path, red: Redactor, cap: int = LOG_CAP):
        super().__init__(daemon=True)
        self.pipe, self.red, self.cap = pipe, red, cap
        self.f = open(path, "ab", buffering=0)
        self.tail = b""
        self.written = 0
        self.truncated = False

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        self.tail = (self.tail + data)[-TAIL:]
        if self.written >= self.cap:
            self.truncated = True
            return
        room = self.cap - self.written
        if len(data) > room:
            data = data[:room]
            self.truncated = True
        self.f.write(data)
        self.written += len(data)

    def run(self) -> None:
        carry = b""
        keep = max(0, self.red.maxlen - 1)
        while True:
            chunk = self.pipe.read1(65536) if hasattr(self.pipe, "read1") else self.pipe.read(65536)
            if not chunk:
                break
            buf = self.red.bytes(carry + chunk)
            if keep and len(buf) > keep:
                self._emit(buf[:-keep])
                carry = buf[-keep:]
            elif keep:
                carry = buf
            else:
                self._emit(buf)
        self._emit(self.red.bytes(carry))
        if self.truncated:
            self.f.write(b"\n[bench: output truncated at %d bytes]\n" % self.cap)
        self.f.close()


def read_capped(path: Path, cap: int = LOG_CAP) -> str:
    with open(path, "rb") as f:
        return f.read(cap).decode(errors="replace")


# ------------------------------------------------------------------ process tree
def _proc_scan(match: Callable[[int, int, bytes], bool]) -> list[int]:
    pids = []
    if not Path("/proc/self").exists():
        return pids
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            uid = os.stat(f"/proc/{d}").st_uid
            env = Path(f"/proc/{d}/environ").read_bytes() if os.geteuid() == 0 or uid == os.geteuid() else b""
        except OSError:
            continue
        if match(int(d), uid, env):
            pids.append(int(d))
    return pids


def descendants_of(root: int) -> set[int]:
    """Portable (ps) snapshot of root's descendants; used to catch setsid/double-fork escapees that
    are later reparented to init and therefore invisible by ancestry."""
    try:
        txt = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    kids: dict[int, list[int]] = {}
    for line in txt.splitlines():
        try:
            pid, ppid = (int(x) for x in line.split())
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
    out, stack = set(), [root]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in out:
                out.add(k)
                stack.append(k)
    return out


def kill_tree(pgid: int | None, mark: str, agent_uid: int | None, seen: set[int] | None = None) -> int:
    """SIGKILL the process group, then everything that escaped it: processes of the agent
    uid, or (Linux) carrying our per-trial env marker. Repeats until nothing is left."""
    killed = 0
    for _ in range(5):
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        for pid in list(seen or ()):
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        victims = _proc_scan(lambda pid, uid, env: (agent_uid is not None and uid == agent_uid and os.geteuid() == 0)
                             or (mark.encode() in env))
        for v in victims:
            try:
                os.kill(v, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        if not victims:
            break
        time.sleep(0.2)
    return killed


def purge_credentials(home: Path) -> None:
    """Delete every credential-bearing file the adapters wrote into the private trial home."""
    import fnmatch
    for f in list(home.rglob("*")):
        try:
            if f.is_file() and any(fnmatch.fnmatch(f.name, g) for g in SECRET_FILE_GLOBS):
                f.write_bytes(b"")
                f.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ trial
class TrialParams:
    def __init__(self, **kw: Any):
        self.run_id = kw["run_id"]
        self.attempt_id = kw["attempt_id"]
        self.task_id = kw["task_id"]
        self.harness = kw["harness"]
        self.profile_mib = int(kw["profile_mib"])
        self.timeout_s = int(kw["timeout_s"])
        self.stage = kw.get("stage", "scored")
        self.out = Path(kw["out"])
        self.work = Path(kw.get("work", "/work"))
        self.fixtures = Path(kw.get("fixtures", FIXTURES_DIR))
        self.fixture_rel = kw["fixture_rel"]          # e.g. fixtures/mini-calc
        self.prompt = kw["prompt"]
        self.cfg = kw["cfg"]                          # frozen config dict
        self.agent_uid = kw.get("agent_uid", AGENT_UID if os.geteuid() == 0 else None)
        self.snapshot_seconds = float(kw.get("snapshot_seconds", 60))
        self.sample_seconds = float(kw.get("sample_seconds", 1.0))
        self.enforce_cgroup_limit = kw.get("enforce_cgroup_limit", True)


def _atomic_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


def _chown_tree(root: Path, uid: int) -> None:
    if os.geteuid() != 0:
        return
    os.chown(root, uid, uid)
    for p in root.rglob("*"):
        os.lchown(p, uid, uid)


def _tar_workspace(src: Path, dest: Path) -> None:
    """Archive of the already-redacted, safe-copied workspace (never of the raw agent tree: gzip would
    bake any secret in beyond redaction). Regular files only by construction."""
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src, arcname="workspace", recursive=True)


def run_trial(p: TrialParams, *, cg: cgm.Cgroup | None = None,
              build: Callable[[str, H.Ctx], H.Invocation] | None = None) -> dict[str, Any]:
    out = p.out
    out.mkdir(parents=True, exist_ok=True)
    cfg = p.cfg
    key_env = cfg["model"]["api_key_env"]
    secret = os.environ.get(key_env, "")
    red = Redactor([secret])
    t_start = time.time()
    mark = f"BENCH_MARK_{uuid.uuid4().hex}"
    result: dict[str, Any] = {
        "schema": 1, "run_id": p.run_id, "attempt_id": p.attempt_id, "task_id": p.task_id, "harness": p.harness,
        "profile_mib": p.profile_mib, "stage": p.stage, "timeout_s": p.timeout_s, "complete": False,
        "started_at": t_start, "worker_pid": os.getpid(),
        "limits_disclosure": cfg.get("limits_disclosure"),
        "supervisor_note": "worker supervisor shares the container cgroup; its footprint is included in cgroup numbers",
        "harness_compat": H.COMPAT.get(p.harness),
        "versions": {"harness": cfg.get("harnesses", {}).get(p.harness, {}).get("version"),
                     "loop_commit": cfg.get("loop", {}).get("commit"),
                     "image": cfg.get("images", {}).get("runner")},
        "model": {"base_url": cfg["model"]["base_url"], "name": cfg["model"]["name"], "provider_id": cfg["model"]["provider_id"]},
    }
    (out / "heartbeat").write_text(str(time.time()))
    _atomic_json(out / "result.partial.json", result)
    seen_pids: set[int] = set()
    cancelled = threading.Event()
    agent_proc: dict[str, Any] = {"proc": None}

    def on_term(signum, frame):  # pod deletion / cancel
        cancelled.set()
        proc = agent_proc["proc"]
        kill_tree(proc.pid if proc else None, mark, p.agent_uid, seen_pids)

    old_term = None
    try:
        old_term = signal.signal(signal.SIGTERM, on_term)
    except ValueError:
        pass  # not main thread (tests)
    try:
        os.chmod(out, 0o700)   # agent uid must never read/write artifacts
    except OSError:
        pass

    sampler: cgm.Sampler | None = None
    try:
        cg = cg or cgm.Cgroup()
        val = cg.validate(p.profile_mib * 1024 * 1024, float(cfg["experiment"]["cpu"])) if p.enforce_cgroup_limit else \
            {"ok": True, "reasons": [], "detect": cg.info, "snapshot": cg.snapshot()}
        snap0 = cg.snapshot()
        result["cgroup"] = {"valid": val["ok"], "reasons": val["reasons"], "version": cg.info.get("version"),
                            "detect": cg.info, "start": snap0}
        # ---- workspace
        project = p.work / "project"
        home = p.work / "home"
        if project.exists():
            shutil.rmtree(project)
        shutil.copytree(p.fixtures.parent / p.fixture_rel if not Path(p.fixture_rel).is_absolute() else p.fixture_rel,
                        project, ignore=shutil.ignore_patterns("node_modules", "target", "__pycache__", ".git"), symlinks=False)
        # The image keeps fixtures read-only; the agent's private working copy must be writable or every
        # harness wastes steps (or fails) on EACCES before it can edit anything.
        from .verification import make_tree_writable
        make_tree_writable(project)
        home.mkdir(parents=True, exist_ok=True)
        if p.agent_uid is not None:
            _chown_tree(project, p.agent_uid)
            _chown_tree(home, p.agent_uid)
        ctx = H.Ctx(workdir=project, home=home, prompt=p.prompt, base_url=cfg["model"]["base_url"], model=cfg["model"]["name"],
                    provider_id=cfg["model"]["provider_id"], api_key_env=key_env, loop_commit=cfg.get("loop", {}).get("commit", ""),
                    started_ms=int(time.time() * 1000) - 1000)
        inv = (build or H.build)(p.harness, ctx)
        if p.agent_uid is not None:
            _chown_tree(home, p.agent_uid)
        inv.env[mark] = "1"
        result["command_redacted"] = H.redacted_cmd(inv.cmd, [secret])
        result["invocation_notes"] = inv.notes

        sampler = cgm.Sampler(cg, out / "resource_samples.jsonl", p.sample_seconds)
        sampler.sample_once()
        sampler.start()

        snap_stop = threading.Event()

        def snapshot_loop():
            while not snap_stop.wait(p.snapshot_seconds):
                try:
                    tmp = out / "workspace-live.tmp"
                    shutil.rmtree(tmp, ignore_errors=True)
                    safe_copy(project, tmp, max_total=64 << 20)
                    redact_tree(tmp, red)
                    live = out / "workspace-live"
                    shutil.rmtree(live, ignore_errors=True)
                    os.replace(tmp, live)
                    (out / "heartbeat").write_text(str(time.time()))
                except Exception:
                    pass
        threading.Thread(target=snapshot_loop, daemon=True).start()

        # ---- run the agent
        agent: dict[str, Any] = {"exit_code": None, "signal": None, "launch_failed": False}
        t0 = time.monotonic()
        timed_out = False
        kw: dict[str, Any] = {}
        if p.agent_uid is not None:
            kw.update(user=p.agent_uid, group=p.agent_uid, extra_groups=[])
        try:
            proc = subprocess.Popen(inv.cmd, cwd=project, env=inv.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, **kw)
            agent_proc["proc"] = proc
            po = Pump(proc.stdout, out / "agent.stdout.log", red)
            pe = Pump(proc.stderr, out / "agent.stderr.log", red)
            po.start(), pe.start()
            deadline = t0 + p.timeout_s
            last_scan = 0.0
            while proc.poll() is None:
                if time.monotonic() - last_scan > 1.0:
                    seen_pids |= descendants_of(proc.pid)
                    last_scan = time.monotonic()
                if cancelled.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.2)
            if timed_out or cancelled.is_set():
                seen_pids |= descendants_of(proc.pid)
                kill_tree(proc.pid, mark, p.agent_uid, seen_pids)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            rc = proc.returncode
            agent["exit_code"] = rc if rc is not None and rc >= 0 else None
            agent["signal"] = -rc if rc is not None and rc < 0 else None
            leftovers = kill_tree(proc.pid, mark, p.agent_uid, seen_pids)   # background servers etc. must not outlive the agent
            agent["descendants_killed_after_exit"] = bool(leftovers) and not (timed_out or cancelled.is_set())
            po.join(5), pe.join(5)
            agent["stdout_truncated"], agent["stderr_truncated"] = po.truncated, pe.truncated
            agent["stdout_tail"] = po.tail.decode(errors="replace")
            agent["stderr_tail"] = pe.tail.decode(errors="replace")
        except (FileNotFoundError, PermissionError, OSError) as e:
            agent.update(launch_failed=True, launch_error=red(str(e)))
        wall = time.monotonic() - t0
        snap_stop.set()

        # ---- counters
        sampler.sample_once()
        snap1 = cg.snapshot()
        sampler.stop()
        d_oom = None
        if snap1.get("oom_kill") is not None:
            d_oom = snap1["oom_kill"] - (snap0.get("oom_kill") or 0)
        cpu_ns = None if snap1.get("cpu_usage_ns") is None else snap1["cpu_usage_ns"] - (snap0.get("cpu_usage_ns") or 0)
        result["cgroup"].update({
            "end": snap1,
            "oom_kill_delta": d_oom, "mem_failcnt": snap1.get("mem_failcnt"),
            "peak_memory_bytes": snap1.get("mem_peak"),
            "peak_memory_note": "kernel peak counter; includes reclaimable page cache" if cg.info.get("version") == "v1" else
                                ("memory.peak" if snap1.get("mem_peak") is not None else "null: memory.peak unavailable"),
            "sampled_peak_rss_bytes": sampler.peak_rss, "sampled_peak_working_set_bytes": sampler.peak_ws,
            "sampled_peak_usage_bytes": sampler.peak_usage, "n_samples": sampler.n,
            "cpu_seconds": None if cpu_ns is None else cpu_ns / 1e9,
            "cpu_nr_throttled": snap1.get("cpu_nr_throttled"), "cpu_nr_periods": snap1.get("cpu_nr_periods"),
            "cpu_throttled_seconds": None if snap1.get("cpu_throttled_ns") is None else snap1["cpu_throttled_ns"] / 1e9,
        })
        agent.update(wall_seconds=wall)
        result.update(agent=agent, timed_out=timed_out, cancelled=cancelled.is_set(), wall_seconds=wall)

        # ---- usage (reused adapters; None when the harness did not record it)
        metrics = None
        try:
            so = read_capped(out / "agent.stdout.log") if (out / "agent.stdout.log").exists() else ""
            metrics = inv.scrape(so, agent.get("stderr_tail") or "")
        except Exception as e:   # adapter failure must not kill the trial
            result["usage_error"] = red(f"{type(e).__name__}: {e}")
        result["usage"] = metrics if metrics else None
        result["usage_missing"] = metrics is None

        # ---- artifacts. Order matters: copy -> redact -> archive/diff (never archive pre-redaction bytes).
        # Auth/config stores (models.json, providers.json, config.yaml, ...) are excluded from the export
        # entirely and then purged from the private trial home.
        sess = out / "harness_state"
        rep_state = safe_copy(home, sess, max_total=64 << 20,
                              skip_dirs=(".cache", ".npm", ".cargo-home", "node_modules", "target"),
                              skip_file_globs=SECRET_FILE_GLOBS, skip_dir_names=SECRET_DIR_NAMES)
        result["harness_state_copy"] = rep_state.as_dict()
        sub = out / "submission"
        shutil.rmtree(sub, ignore_errors=True)
        rep: CopyReport = safe_copy(project, sub)
        result["submission_copy"] = rep.as_dict()
        n_red = redact_tree(out, red)
        result["redacted_files"] = n_red
        try:
            _tar_workspace(sub, out / "workspace-full.tar.gz")
        except Exception as e:
            result["archive_error"] = str(e)
        fx = (p.fixtures.parent / p.fixture_rel) if not Path(p.fixture_rel).is_absolute() else Path(p.fixture_rel)
        d = subprocess.run(["diff", "-ruN", "-x", "node_modules", "-x", "target", "-x", ".git", str(fx), str(sub)],
                           capture_output=True, text=True, errors="replace")
        (out / "diff.patch").write_text(red(d.stdout))
        purge_credentials(home)
        shutil.rmtree(out / "workspace-live", ignore_errors=True)
    except BaseException as e:  # noqa: BLE001 - everything becomes a recorded infra failure
        result["worker_error"] = red(f"{type(e).__name__}: {e}")
        if sampler:
            sampler.stop()
    finally:
        if old_term is not None:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except ValueError:
                pass
        result["finished_at"] = time.time()
        result["complete"] = True
        _atomic_json(out / "result.json", result)
        (out / "COMPLETE").write_text(str(time.time()))
    return result


# ------------------------------------------------------------------ verify entrypoint
def run_verify(task_id: str, trial_dir: Path, out_dir: Path) -> dict[str, Any]:
    from . import verification  # owned by the verifier workstream
    out_dir.mkdir(parents=True, exist_ok=True)
    sub = trial_dir / "submission"
    res = verification.verify(task_id, sub, out_dir)
    # the verifier pod exits 0 whenever it produced a verdict; pass/fail is data
    _atomic_json(out_dir / "verdict.json", res)
    (out_dir / "COMPLETE").write_text(str(time.time()))
    return res


# ------------------------------------------------------------------ probe entrypoint
def run_probe() -> dict[str, Any]:
    mib = int(os.environ["BENCH_PROFILE_MIB"])
    cg = cgm.Cgroup()
    val = cg.validate(mib * 1024 * 1024, 1.0)
    before = cg.snapshot()
    ballast = bytearray(min(64, max(8, mib // 4)) * 1024 * 1024)
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1
    after = cg.snapshot()
    grew = (after["mem_peak"] is not None and before["mem_peak"] is not None and after["mem_peak"] - before["mem_peak"] >= len(ballast) * 0.9) or \
           (after["mem_rss"] is not None and before["mem_rss"] is not None and after["mem_rss"] - before["mem_rss"] >= len(ballast) * 0.9)
    reasons = list(val["reasons"])
    if not grew:
        reasons.append("allocating ballast did not move the memory counters: counters are not tracking this container")
    # bounded CPU burn: two child processes spin for ~2s against the 1-core quota (exercises throttling)
    busy = "import time\nt=time.time()+2.0\nwhile time.time()<t: pass"
    kids = [subprocess.Popen([sys.executable, "-c", busy]) for _ in range(2)]
    [k.wait(timeout=30) for k in kids]
    end = cg.snapshot()
    if end["cpu_usage_ns"] is not None and before["cpu_usage_ns"] is not None and end["cpu_usage_ns"] <= before["cpu_usage_ns"]:
        reasons.append("cpu usage counter did not advance")
    if end["cpu_nr_throttled"] is not None and before["cpu_nr_throttled"] is not None and \
            end["cpu_nr_throttled"] <= before["cpu_nr_throttled"] and (end["cpu_quota_us"] or 0) > 0:
        reasons.append("2 busy processes on a 1-core quota did not register cpu throttling")
    res = {"ok": not reasons, "reasons": reasons, "detect": cg.info, "before": before, "after": after, "end": end,
           "ballast_bytes": len(ballast), "profile_mib": mib}
    print("BENCH_PROBE_RESULT " + json.dumps(res, sort_keys=True))
    return res


def _trial_from_env() -> TrialParams:
    from .tasks import get_task
    cfg = json.loads(Path("/etc/bench/config.json").read_text())
    task = get_task(os.environ["BENCH_TASK"])
    return TrialParams(run_id=os.environ["BENCH_RUN_ID"], attempt_id=os.environ["BENCH_ATTEMPT_ID"], task_id=task.id,
                       harness=os.environ["BENCH_HARNESS"], profile_mib=os.environ["BENCH_PROFILE_MIB"],
                       timeout_s=os.environ["BENCH_TIMEOUT_S"], stage=os.environ.get("BENCH_STAGE", "scored"),
                       out="/out", work="/work", fixture_rel=task.fixture, prompt=task.prompt, cfg=cfg,
                       snapshot_seconds=cfg.get("timeouts", {}).get("snapshot_seconds", 60))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else ""
    if mode == "trial":
        r = run_trial(_trial_from_env())
        return 0 if not r.get("worker_error") else 3
    if mode == "verify":
        # verifier pod: /trial (ro) -> /verify-out ; clean env, no credentials
        run_verify(os.environ["BENCH_TASK"], Path("/trial"), Path("/verify-out"))
        return 0
    if mode == "probe":
        return 0 if run_probe()["ok"] else 4
    print("usage: python -m bench.worker trial|verify|probe", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
