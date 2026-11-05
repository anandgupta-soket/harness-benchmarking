#!/usr/bin/env python3
"""Run one bench task against a harness and append a CSV row.

Supports ten harnesses, dispatched by `--harness`:

- loop: shells `loop --print`, scrapes tokens/turns from
  ~/.loop/agent/sessions.db (Loop's own SQLite session store).
- deepseek-harness: runs scripts/dsh_sdk_run.py under a Python that has
  deepseek-harness-sdk (the SDK path eval-bench's adapter already runs; the
  `dsh --profile headless` CLI has no provider/model flags), with a per-run
  DSH_HOME. Tokens/turns/tool calls come from the session.v3.jsonl trace
  (`assistant/message` usage entries and `tool/call` entries).
- goose: shells `goose run -t <prompt> -q`, scrapes tokens/turns/cost from
  goose's own sessions.db (SQLite, same storage style as Loop as of goose
  1.10.0+). The db's location is asked from `goose info` rather than
  guessed, since goose's cross-platform data-dir convention isn't ours to
  assume; the session this run created is identified by diffing the set of
  session ids before/after the call (no shared timestamp format to filter
  on, unlike Loop's millisecond-epoch created_at).
- pi: shells `pi --print <prompt>`, scrapes tokens/turns/cost from the
  session .jsonl file pi writes under a per-run PI_CODING_AGENT_SESSION_DIR
  this script creates and points it at (same isolation trick as DSH_HOME -
  pi's own docs name this exact env var and say the CLI flag/env var take
  precedence over its default ~/.pi/agent/sessions/ location).

- opencode: shells `opencode run --standalone --auto --format json`, takes
  this run's session id from the `sessionID` carried on every stdout event
  (deterministic - concurrent runs can't mix up), then reads aggregate
  tokens/cost/turns from `opencode session export <id> --standalone`. Shapes
  confirmed against the real v2.0.20 binary; the public docs describe an
  older CLI (e.g. no `db path` subcommand exists in 2.0.20).

- hermes: `hermes -z <prompt> --yolo --ignore-rules --usage-file`, with a fresh
  HERMES_HOME per run (Hermes learns and remembers across sessions, so a shared
  home would leak state between tasks). Tokens/cost/turns come from its usage
  JSON, tool calls from `role='tool'` rows in its state.db.

- kilo: `kilo run --auto --format json` (an OpenCode fork); every number comes
  from its own stdout events (`step_finish` tokens/cost, `tool_use` calls).

- cline: `cline --json` with per-run --data-dir/--config; metrics from its
  `run_result` event (usage, iterations) and `tool_call` hook events.

- omp: `omp --print --auto-approve` (oh-my-pi, a Pi-family agent that needs Bun);
  per-run --session-dir, read with the same parser as pi.

- codex: `codex exec --json` pointed at OpenRouter through a declared custom
  provider, with a fresh CODEX_HOME per run; metrics from its session rollout
  (one token_count per model request, one function_call per tool call).

All ten also get peak memory (resource.getrusage(RUSAGE_CHILDREN).ru_maxrss,
delta across the subprocess call) recorded alongside tokens/turns/wall clock.

Running several `run_task.py` invocations at once (different OS processes,
e.g. from a shell loop or a wrapper script - this script has no built-in
concurrency of its own) is safe for all four harnesses:

- Each fixture-backed run works in its own temp copy of the fixture
  (git-reset to HEAD, then copied - node_modules/target/__pycache__
  excluded), not the shared fixtures/<name> directory in this repo. The
  agent's subprocess cwd (and, for loop, its --cwd flag) points at that
  copy, and the grading command/file_contains path is remapped onto the
  same copy, so two runs editing the "same" fixture concurrently can't
  collide on disk, and the harness's own tools resolve bare filenames in
  task prompts correctly (confirmed `loop --cwd` controls this; the other
  three harnesses use the subprocess's OS cwd directly).
- loop and pi get a fresh, isolated per-run session-storage directory
  (LOOP_CODING_AGENT_SESSION_DIR / PI_CODING_AGENT_SESSION_DIR, both
  confirmed to actually relocate where the real binary writes its session
  db/file - `loop config` was used to verify this directly, not just
  assumed from --help text), same as deepseek-harness's existing DSH_HOME.
- goose has no equivalent narrow override (its only full-relocation knob,
  GOOSE_PATH_ROOT, also moves its config/auth, so isolating sessions that
  way would break credential lookup) - concurrent goose runs instead
  serialize against each other through a cross-process file lock
  (runs/.goose.lock) around the run-and-scrape step, so they can't race on
  "which session did I just create." They still run fully concurrently
  with loop/dsh/pi runs; only goose-vs-goose is serialized.

Usage:
  python3 scripts/run_task.py WEB-BUG-01
  python3 scripts/run_task.py tasks/core-coding/web-bug-01-fix-remaining.yaml
  python3 scripts/run_task.py --harness deepseek-harness MY-TASK --model deepseek-chat
  python3 scripts/run_task.py --harness goose MY-TASK --provider openrouter --model <model>
  python3 scripts/run_task.py --harness pi MY-TASK --provider openrouter --model <model>
  python3 scripts/run_task.py --harness opencode MY-TASK --provider openrouter --model <model>
  python3 scripts/run_task.py --self-test

Environment:
  LOOP_BIN       Command prefix for --harness loop (default: loop). Example:
                 LOOP_BIN='cargo run -p loop-cli --features orchestration --'
  LOOP_PROVIDER  Forwarded as --provider (loop only)
  LOOP_MODEL     Forwarded as --model (loop only; --model flag wins if both set)
  LOOP_CODING_AGENT_DIR / LOOP_CODING_AGENT_SESSION_DIR
                 Same path overrides the Loop CLI uses for sessions.db
  DSH_PYTHON     Python with deepseek-harness-sdk for --harness deepseek-harness
                 (default: this interpreter if it has the SDK, else ../eval-bench/.venv)
  GOOSE_BIN      Command prefix for --harness goose (default: goose)
  PI_BIN         Command prefix for --harness pi (default: pi)
  HERMES_BIN     Command prefix for --harness hermes (default: hermes)
  KILO_BIN       Command prefix for --harness kilo (default: kilo)
  CLINE_BIN      Command prefix for --harness cline (default: cline)
  OMP_BIN        Command prefix for --harness omp (default: omp)
  CODEX_BIN      Command prefix for --harness codex (default: codex)
  OPENCODE_BIN   Command prefix for --harness opencode (default: opencode)
  DEEPSEEK_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY / ...
                 Whichever key the harness's configured provider needs - this
                 script doesn't set API keys itself, only per-run session dirs
                 (DSH_HOME, PI_CODING_AGENT_SESSION_DIR, LOOP_CODING_AGENT_SESSION_DIR)
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import json
import os
import re
import resource
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_tasks import ROOT, load_suite_index, parse_simple_yaml

CSV_COLUMNS = (
    "run_id",
    "date",
    "harness",
    "model",
    "task_id",
    "repo_sha",
    "success",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "t_first_token_ms",
    "t_last_token_ms",
    "wall_clock_ms",
    "peak_rss_kb",
    "cpu_user_s",
    "cpu_sys_s",
    "assistant_turns",
    "tool_calls",
    "retries",
    "notes",
)

# Single source of truth for valid --harness values (run_suite.py imports it).
HARNESS_CHOICES = ("loop", "deepseek-harness", "goose", "pi", "opencode", "hermes", "kilo", "cline", "omp", "codex")

DEFAULT_CSV = ROOT / "runs" / "runs.csv"
TEMPLATE_CSV = ROOT / "runs" / "template.csv"


def sessions_db_path(override_dir: Path | None = None) -> Path:
    """Resolve Loop's sessions.db location. `override_dir` (this run's own
    isolated LOOP_CODING_AGENT_SESSION_DIR, confirmed via `loop config` to
    actually control where the real binary writes it) always wins; the env
    var fallbacks below only matter when scraping without that isolation.
    """
    if override_dir is not None:
        return override_dir / "sessions.db"
    session_dir = os.environ.get("LOOP_CODING_AGENT_SESSION_DIR", "").strip()
    if session_dir:
        return Path(session_dir) / "sessions.db"
    agent_dir = os.environ.get("LOOP_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        return Path(agent_dir) / "sessions.db"
    return Path.home() / ".loop" / "agent" / "sessions.db"


def metrics_from_entry_payloads(payloads: list[str]) -> dict[str, Any]:
    """Sum usage from serialized SessionTreeEntry JSON blobs."""
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    assistant_turns = 0
    tool_calls = 0
    model = ""

    for raw in payloads:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        assistant_turns += 1
        if not model:
            model = str(msg.get("model") or "")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        input_tokens += int(usage.get("input") or 0)
        output_tokens += int(usage.get("output") or 0)
        cache_read += int(usage.get("cacheRead") or usage.get("cache_read") or 0)
        cache_write += int(usage.get("cacheWrite") or usage.get("cache_write") or 0)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    tool_calls += 1

    prompt = input_tokens + cache_read + cache_write
    return {
        "session_id": "",
        "input_tokens": prompt,
        "output_tokens": output_tokens,
        "total_tokens": prompt + output_tokens,
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "model": model,
    }


def scrape_session_metrics(created_after_ms: int, session_dir: Path | None = None) -> dict[str, Any] | None:
    """Read the newest session created during this run from Loop's SQLite
    store. `session_dir` is this run's isolated LOOP_CODING_AGENT_SESSION_DIR
    when set, in which case the whole db belongs to this run alone and
    created_after_ms is just a harmless extra safety net, not load-bearing.
    """
    db = sessions_db_path(session_dir)
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (created_after_ms,),
        ).fetchone()
        if not row:
            return None
        session_id = str(row[0])
        payloads = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM session_entries WHERE session_id=? ORDER BY entry_seq",
                (session_id,),
            ).fetchall()
        ]
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    metrics = metrics_from_entry_payloads(payloads)
    metrics["session_id"] = session_id
    return metrics


def metrics_from_dsh_usage_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive CSV-shaped metrics from a DeepSeek Harness session.v3.jsonl.

    Shape confirmed on a real run (sdk-minimal profile, OpenRouter): each LLM
    call is an `assistant/message` entry with `data.usage` = {inputTokens,
    outputTokens, totalTokens, cacheReadTokens?}; inputTokens EXCLUDES cache
    reads (21 + 1088 cached + 125 out == totalTokens 1234), so cache folds
    into input_tokens like every other harness. One assistant/message = one
    turn; tool calls are `tool/call` entries.

    An older/other trace flavor with standalone top-level {"type": "usage",
    "usage": {...}} entries is still understood when no assistant/message
    entries are present.
    """
    messages = [e for e in entries if e.get("type") == "assistant/message"]
    if messages:
        usages = [
            (e.get("data") or {}).get("usage") if isinstance((e.get("data") or {}).get("usage"), dict) else {}
            for e in messages
        ]
        tool_calls: int | None = sum(1 for e in entries if e.get("type") == "tool/call")
    else:
        legacy = [e for e in entries if e.get("type") == "usage"]
        usages = [e["usage"] if isinstance(e.get("usage"), dict) else {} for e in legacy]
        tool_calls = None  # the legacy flavor doesn't break tool calls out
    input_tokens = sum(int(u.get("inputTokens") or 0) + int(u.get("cacheReadTokens") or 0) for u in usages)
    output_tokens = sum(int(u.get("outputTokens") or 0) for u in usages)
    return {
        "session_id": "",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "assistant_turns": len(usages),
        "tool_calls": tool_calls,
        "model": "",
    }


def scrape_dsh_metrics(dsh_home: Path) -> dict[str, Any] | None:
    """Find and parse the session.v*.jsonl trace dsh wrote under DSH_HOME
    (rglob: it nests under sessions/--<cwd>--/<session>/).
    """
    # The session log was session.v3.jsonl through SDK 0.1.x and is session.v4.jsonl from
    # 0.2.x; the usage / tool-call entry shapes are the same, so accept any version.
    traces = sorted(dsh_home.rglob("session.v*.jsonl"))
    if not traces:
        return None
    entries: list[dict[str, Any]] = []
    for line in traces[0].read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    if not any(e.get("type") in ("assistant/message", "usage") for e in entries):
        return None
    return metrics_from_dsh_usage_entries(entries)


def goose_sessions_db_path(goose_cmd_prefix: list[str]) -> Path | None:
    """Ask goose itself where its sessions.db lives via `goose info`, rather
    than reimplementing its cross-platform data-dir convention (it resolves
    to a different path per OS, and can be overridden by GOOSE_PATH_ROOT -
    not ours to assume or hardcode).
    """
    try:
        proc = subprocess.run(
            [*goose_cmd_prefix, "info"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    for line in proc.stdout.splitlines():
        if "Sessions DB" not in line:
            continue
        parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
        if len(parts) >= 2:
            return Path(parts[1])
    return None


def list_goose_session_ids(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        return {str(r[0]) for r in conn.execute("SELECT id FROM sessions").fetchall()}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def metrics_from_goose_session_row(
    row: tuple[Any, ...], assistant_turns: int, tool_calls: int | None = None
) -> dict[str, Any]:
    """Derive CSV-shaped metrics from one `sessions` table row's
    accumulated_* columns - the lifetime totals for the session, unlike the
    table's plain total_tokens/input_tokens columns which track only the
    current context window and can drop after a mid-session compaction.

    Cache tokens are folded into input_tokens the same way Loop's own
    metrics_from_entry_payloads does, so input_tokens/total_tokens carry the
    same meaning in the CSV regardless of which harness wrote the row.
    """
    acc_input, acc_output, acc_cache_read, acc_cache_write, acc_cost = row
    prompt = (acc_input or 0) + (acc_cache_read or 0) + (acc_cache_write or 0)
    output = acc_output or 0
    return {
        "session_id": "",
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": prompt + output,
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "model": "",
        "cost_usd": acc_cost,
    }


def scrape_goose_metrics(db_path: Path, known_session_ids: set[str]) -> dict[str, Any] | None:
    """Find the session this run created (the newest id not already in
    known_session_ids, captured right before the subprocess call) and sum
    its usage. No shared timestamp format to filter on across platforms, so
    this uses a before/after id-set diff instead of Loop's millisecond
    created_at>=threshold approach.
    """
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        recent = conn.execute("SELECT id FROM sessions ORDER BY created_at DESC LIMIT 20").fetchall()
        session_id = next((str(r[0]) for r in recent if str(r[0]) not in known_session_ids), None)
        if session_id is None:
            return None
        row = conn.execute(
            """SELECT accumulated_input_tokens, accumulated_output_tokens,
                      accumulated_cache_read_tokens, accumulated_cache_write_tokens,
                      accumulated_cost
               FROM sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        turns_row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchone()
        # Confirmed on a real tool-using session: assistant content_json
        # blocks are "text" and "toolRequest" (the matching "toolResponse"
        # comes back as a user-role message and is not counted twice).
        tool_calls = 0
        for (content_json,) in conn.execute(
            "SELECT content_json FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ):
            try:
                blocks = json.loads(content_json)
            except json.JSONDecodeError:
                continue
            if isinstance(blocks, list):
                tool_calls += sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "toolRequest")
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    metrics = metrics_from_goose_session_row(row, turns_row[0] if turns_row else 0, tool_calls)
    metrics["session_id"] = session_id
    return metrics


def metrics_from_pi_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum usage from a pi session.jsonl's `"type":"message"` entries whose
    nested message.role is "assistant" (per docs/session-format.md: usage
    keys are input/output/cacheRead/cacheWrite/cost.total - the same shape
    Loop's own session store uses, folded into input_tokens the same way).
    Confirmed directly against a real session file, not just the docs: a
    failed/errored call (stopReason "error") still writes an assistant
    message with zeroed usage, so it still counts as one turn here - same
    caveat already noted for goose's equivalent case.
    """
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    cost = 0.0
    assistant_turns = 0
    tool_calls = 0
    model = ""
    for entry in entries:
        if entry.get("type") != "message":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        assistant_turns += 1
        if not model:
            model = str(msg.get("model") or "")
        # Confirmed on a real tool-using pi session: assistant content blocks
        # are "text" and "toolCall".
        if isinstance(msg.get("content"), list):
            tool_calls += sum(1 for b in msg["content"] if isinstance(b, dict) and b.get("type") == "toolCall")
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        input_tokens += int(usage.get("input") or 0)
        output_tokens += int(usage.get("output") or 0)
        cache_read += int(usage.get("cacheRead") or 0)
        cache_write += int(usage.get("cacheWrite") or 0)
        usage_cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        cost += float(usage_cost.get("total") or 0)

    prompt = input_tokens + cache_read + cache_write
    return {
        "session_id": "",
        "input_tokens": prompt,
        "output_tokens": output_tokens,
        "total_tokens": prompt + output_tokens,
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,  # not broken out separately from message content here
        "model": model,
        "cost_usd": cost,
    }


def scrape_pi_metrics(pi_session_dir: Path) -> dict[str, Any] | None:
    """Find the .jsonl session file(s) pi wrote under the per-run session
    dir this script points PI_CODING_AGENT_SESSION_DIR at, and sum usage
    across them. rglob rather than a direct listdir: pi's default location
    nests files under a --<cwd>-- subdirectory (docs/session-format.md),
    and even though PI_CODING_AGENT_SESSION_DIR was confirmed (by a real
    run) to write flat into the directory it's given, rglob stays correct
    either way without depending on that detail holding across versions.
    """
    if not pi_session_dir.is_dir():
        return None
    traces = sorted(pi_session_dir.rglob("*.jsonl"))
    if not traces:
        return None
    entries: list[dict[str, Any]] = []
    for trace in traces:
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    if not entries:
        return None
    return metrics_from_pi_entries(entries)


def metrics_from_opencode_export(data: dict[str, Any]) -> dict[str, Any]:
    """Derive CSV-shaped metrics from `opencode session export <id>` JSON.

    Shape confirmed against a real v2.0.20 export (not the older public docs,
    which describe a different CLI): top-level `info` carries aggregate
    `tokens` {input, output, reasoning, cache{read,write}}, `cost`, `model`
    {id, providerID}, and `outcome`; `messages[]` each have a `type`
    ("user" | "assistant" | "idle" | ...), one "assistant" message per
    LLM step. Reasoning tokens are folded into output_tokens (providers bill
    them as completion tokens); cache read/write folds into input_tokens the
    same way every other harness here does, so the CSV columns mean the
    same thing across harnesses.
    """
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    prompt = int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
    output = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    turns = sum(1 for m in messages if isinstance(m, dict) and m.get("type") == "assistant")
    # Tool calls are assistant content blocks of type "tool" (confirmed on a
    # real tool-using export: block types were reasoning/text/tool).
    tool_calls = sum(
        1
        for m in messages
        if isinstance(m, dict) and m.get("type") == "assistant" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool"
    )
    model = info.get("model") if isinstance(info.get("model"), dict) else {}
    return {
        "session_id": str(info.get("id") or ""),
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": prompt + output,
        "assistant_turns": turns,
        "tool_calls": tool_calls,
        "model": str(model.get("id") or ""),
        "cost_usd": info.get("cost"),
    }


def opencode_session_id_from_stdout(stdout: str) -> str | None:
    """Every event in `opencode run --format json` carries `sessionID`, so
    this run's session is identified deterministically from its own output -
    no before/after diffing needed, and concurrent runs can't mix up.
    """
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("sessionID"):
            return str(event["sessionID"])
    return None


def scrape_opencode_metrics(stdout: str, cmd_prefix: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any] | None:
    session_id = opencode_session_id_from_stdout(stdout)
    if not session_id:
        return None
    try:
        proc = subprocess.run(
            [*cmd_prefix, "session", "export", session_id, "--standalone"],
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return metrics_from_opencode_export(data)


def metrics_from_hermes(usage: dict[str, Any], tool_calls: int | None) -> dict[str, Any]:
    """CSV-shaped metrics from `hermes -z --usage-file` JSON (shape confirmed on
    a real v0.21.4 run) plus a tool-call count from its state.db.

    Main-agent counts are top-level; background helper calls (e.g. session title
    generation) are reported separately under `auxiliary` and are real spend, so
    they are included. Cache read/write folds into input_tokens like every other
    harness; total = input + output (the report's own total excludes reasoning
    tokens, which providers bill inside output).
    """
    aux = usage.get("auxiliary") if isinstance(usage.get("auxiliary"), dict) else {}

    def part(d: dict[str, Any], key: str) -> int:
        return int(d.get(key) or 0)

    prompt = part(usage, "input_tokens") + part(usage, "cache_read_tokens") + part(usage, "cache_write_tokens")
    prompt += part(aux, "input_tokens") + part(aux, "cache_read_tokens") + part(aux, "cache_write_tokens")
    output = part(usage, "output_tokens") + part(aux, "output_tokens")
    cost = float(usage.get("estimated_cost_usd") or 0) + float(aux.get("estimated_cost_usd") or 0)
    return {
        "session_id": str(usage.get("session_id") or ""),
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": prompt + output,
        "assistant_turns": part(usage, "api_calls"),
        "tool_calls": tool_calls,
        "model": str(usage.get("model") or ""),
        "cost_usd": cost,
    }


def scrape_hermes_metrics(hermes_home: Path) -> dict[str, Any] | None:
    usage_path = hermes_home / "usage.json"
    if not usage_path.is_file():
        return None
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(usage, dict):
        return None
    tool_calls: int | None = None
    db = hermes_home / "state.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                # one role='tool' message per tool result == one tool call
                tool_calls = conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'tool'").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            tool_calls = None
    return metrics_from_hermes(usage, tool_calls)


def metrics_from_kilo_events(stdout: str) -> dict[str, Any] | None:
    """Metrics from `kilo run --format json` stdout (real v7.8.3 event shapes):
    every LLM step ends with a `step_finish` event whose part carries
    tokens {input, output, reasoning, cache{read,write}} and cost, and every
    tool invocation is a `tool_use` event. No session export is needed.
    Cache folds into input_tokens and reasoning into output_tokens, matching
    the other harnesses.
    """
    input_tokens = output_tokens = steps = tool_calls = 0
    cost = 0.0
    session_id = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = session_id or str(event.get("sessionID") or "")
        etype = event.get("type")
        if etype == "tool_use":
            tool_calls += 1
        elif etype == "step_finish":
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            steps += 1
            input_tokens += int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
            output_tokens += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
            cost += float(part.get("cost") or 0)
    if steps == 0:
        return None
    return {
        "session_id": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "assistant_turns": steps,
        "tool_calls": tool_calls,
        "model": "",
        "cost_usd": cost,
    }


def metrics_from_cline_events(stdout: str) -> dict[str, Any] | None:
    """Metrics from `cline --json` stdout (real v3.0.68 shapes). The final
    `run_result` event carries aggregateUsage {inputTokens, outputTokens,
    cacheReadTokens, cacheWriteTokens, totalCost}, `iterations` and the model;
    each tool invocation is a `hook_event` with hookEventName "tool_call".
    Cline's inputTokens already INCLUDES cached tokens (a 2-step probe: step 2's
    5,145-token prompt contained the 5,056 read from cache), so cache is not
    added on top, unlike harnesses that report it separately.
    """
    result: dict[str, Any] | None = None
    tool_calls = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "run_result":
            result = event
        elif event.get("type") == "hook_event" and event.get("hookEventName") == "tool_call":
            tool_calls += 1
    if result is None:
        return None
    usage = result.get("aggregateUsage") if isinstance(result.get("aggregateUsage"), dict) else result.get("usage") or {}
    prompt = int(usage.get("inputTokens") or 0)
    output = int(usage.get("outputTokens") or 0)
    model = result.get("model") if isinstance(result.get("model"), dict) else {}
    return {
        "session_id": "",
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": prompt + output,
        "assistant_turns": int(result.get("iterations") or 0),
        "tool_calls": tool_calls,
        "model": str(model.get("id") or ""),
        "cost_usd": float(usage.get("totalCost") or 0),
    }


def metrics_from_codex_rollout(lines: list[str]) -> dict[str, Any] | None:
    """Metrics from a Codex session rollout .jsonl (real v0.160.1 shapes).

    One `event_msg` / `token_count` per model request, carrying
    info.last_token_usage {input_tokens, cached_input_tokens, output_tokens,
    reasoning_output_tokens}; one `response_item` per tool call
    (function_call / custom_tool_call / local_shell_call). In OpenAI semantics
    input_tokens already includes cached tokens and output_tokens includes
    reasoning (verified: total_tokens == input + output), so neither is added
    on top. Codex reports no cost.
    """
    requests = tool_calls = input_tokens = output_tokens = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) and isinstance(event.get("payload"), dict) else {}
        ptype = payload.get("type")
        if ptype == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else None
            last = info.get("last_token_usage") if info and isinstance(info.get("last_token_usage"), dict) else None
            if last:
                requests += 1
                input_tokens += int(last.get("input_tokens") or 0)
                output_tokens += int(last.get("output_tokens") or 0)
        elif event.get("type") == "response_item" and ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            tool_calls += 1
    if requests == 0:
        return None
    return {
        "session_id": "",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "assistant_turns": requests,
        "tool_calls": tool_calls,
        "model": "",
    }


def scrape_codex_metrics(codex_home: Path) -> dict[str, Any] | None:
    rollouts = sorted(codex_home.rglob("rollout-*.jsonl"))
    if not rollouts:
        return None
    lines: list[str] = []
    for r in rollouts:
        lines.extend(r.read_text(encoding="utf-8", errors="replace").splitlines())
    return metrics_from_codex_rollout(lines)


def resolve_task_path(task: str) -> Path:
    candidate = Path(task)
    if not candidate.is_absolute():
        from_root = ROOT / candidate
        from_cwd = Path.cwd() / candidate
        if from_root.is_file():
            return from_root
        if from_cwd.is_file():
            return from_cwd.resolve()
        if candidate.is_file():
            return candidate.resolve()

    for path in load_suite_index():
        if not path.is_file():
            continue
        data = parse_simple_yaml(path.read_text(encoding="utf-8"))
        if str(data.get("id", "")).strip() == task:
            return path

    raise SystemExit(f"task not found: {task}")


def git_sha() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def git_toplevel(path: Path) -> Path | None:
    """Nearest git root for `path` (supports a nested  repo)."""
    cwd = path if path.is_dir() else path.parent
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


@contextlib.contextmanager
def file_lock(name: str):
    """Cross-process mutual exclusion via flock on a file under runs/ -
    works between separate `run_task.py` OS processes (this script has no
    concurrency of its own), unlike a threading.Lock.
    """
    lock_path = ROOT / "runs" / name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def reset_fixture(repo: dict[str, Any]) -> str | None:
    if repo.get("type") != "fixture":
        return None
    rel = str(repo.get("path", "")).strip()
    if not rel:
        return None
    fixture = Path(rel) if Path(rel).is_absolute() else ROOT / rel
    toplevel = git_toplevel(fixture)
    if toplevel is None:
        return f"fixture reset skipped: {fixture} is not in a git repo"
    try:
        tracked = fixture.resolve().relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        tracked = rel
    # `git checkout` takes a whole-repo index lock even when scoped to one
    # path - confirmed directly: two concurrent run_task.py processes
    # resetting different fixtures in the same repo still collided on
    # .git/index.lock. Serialize this step across all harnesses/fixtures;
    # it's quick, so this isn't a real bottleneck for parallel runs.
    with file_lock(".git-checkout.lock"):
        proc = subprocess.run(
            ["git", "checkout", "--", tracked],
            cwd=toplevel,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        return f"fixture reset skipped: {proc.stderr.strip() or proc.stdout.strip() or 'git checkout failed'}"
    return None


_WORKDIR_IGNORE = shutil.ignore_patterns("node_modules", "target", "__pycache__", ".git")


def make_work_dir(fixture_root: Path, task_id: str) -> Path:
    """Copy a (just git-reset) fixture into a fresh per-run temp directory,
    so the agent edits and the grading check both happen on an isolated
    copy rather than the shared fixtures/<name> directory in this repo -
    required for two runs on the same fixture (same or different harness)
    to not race on the same files on disk.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=f"bench-work-{task_id}-"))
    shutil.copytree(fixture_root, work_dir, dirs_exist_ok=True, ignore=_WORKDIR_IGNORE)
    return work_dir


# .lemonharness / .pi: scaffolding an installed pi extension (LemonHarness) writes, not the agent's own edits
_DIFF_EXCLUDES = ("node_modules", "target", ".git", "__pycache__", "package-lock.json", "*.pyc", ".DS_Store", ".lemonharness", ".pi")


def save_quality_snapshot(out_dir: Path, tag: str, task_id: str, fixture_root: Path, work_dir: Path,
                          harness: str, model: str, passed: bool | None) -> None:
    """Record what the agent actually changed, for the quality assessment (QUALITY_DIR).

    Writes <out_dir>/<tag>/<task>.diff (the unified diff of the original fixture against
    the agent's final working copy) and <task>.json (run facts and diff statistics).
    Build output (node_modules, target) and the lockfile the grader's `npm install`
    rewrites are excluded so only the agent's own edits show up.
    """
    dest = out_dir / tag
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["diff", "-ruN"]
    for pat in _DIFF_EXCLUDES:
        cmd.extend(["-x", pat])
    cmd.extend([str(fixture_root), str(work_dir)])
    diff = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout
    (dest / f"{task_id}.diff").write_text(diff, encoding="utf-8")

    files: dict[str, dict[str, int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("diff "):
            parts = line.split()
            path = parts[-1]
            try:
                current = str(Path(path).relative_to(work_dir))
            except ValueError:
                current = path
            files[current] = {"added": 0, "removed": 0}
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current]["added"] += 1
        elif current and line.startswith("-") and not line.startswith("---"):
            files[current]["removed"] += 1

    def is_test(rel: str) -> bool:
        low = rel.lower()
        return (low.startswith(("tests/", "test/")) or "/tests/" in low or ".test." in low
                or low.endswith(("_test.rs", "_test.py", "test.js")))

    info = {
        "tag": tag, "task_id": task_id, "harness": harness, "model": model, "passed": passed,
        "files_changed": len(files),
        "lines_added": sum(f["added"] for f in files.values()),
        "lines_removed": sum(f["removed"] for f in files.values()),
        "new_files": sorted(r for r in files if not (fixture_root / r).exists()),
        "test_files_modified": sorted(r for r in files if is_test(r) and (fixture_root / r).exists()),
        "files": files,
    }
    (dest / f"{task_id}.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def remap_fixture_path(path: Path, fixture_root: Path | None, work_dir: Path | None) -> Path:
    """Rewrite a path that lives under the permanent, git-tracked
    fixture_root onto this run's isolated work_dir copy, so a success check
    (file_contains path, or a command's cwd) grades what the agent actually
    edited rather than the pristine original. Paths outside fixture_root
    (or when isolation isn't active) pass through unchanged.
    """
    if fixture_root is None or work_dir is None:
        return path
    try:
        rel = path.resolve().relative_to(fixture_root.resolve())
    except ValueError:
        return path
    return work_dir / rel


@contextlib.contextmanager
def goose_run_lock():
    """Serialize the run-and-scrape step across concurrent `--harness goose`
    processes. goose has no per-run session-dir override that doesn't also
    relocate its config/auth (unlike loop/pi/dsh), so two goose runs writing
    to the same shared sessions.db at once could both claim the same
    "newest session" as their own. This only blocks goose against goose;
    other harnesses proceed fully concurrently.
    """
    with file_lock(".goose.lock"):
        yield


def summarize_loop_stderr(stderr: str | None, returncode: int) -> str:
    """Keep a short, useful Loop failure reason (avoid dumping HTML bodies)."""
    text = (stderr or "").strip()
    if not text:
        return f"loop exit {returncode}"
    http_status = ""
    marker = "HTTP "
    idx = text.find(marker)
    if idx >= 0:
        digits = []
        for ch in text[idx + len(marker) :]:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        http_status = "".join(digits)
    lowered = text.lower()
    htmlish = "</html>" in lowered or "<!doctype html" in lowered or "<html" in lowered
    if htmlish:
        if http_status:
            return f"HTTP {http_status} (HTML error page from API)"
        return "API returned an HTML error page"
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("loop:"):
            return stripped[:300]
    return text.splitlines()[-1].strip()[:300]


def csv_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


def csv_float(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return ""


def ensure_csv(path: Path) -> None:
    """Create the CSV with the current header, or upgrade an existing file
    whose header predates newer columns (e.g. peak_rss_kb, cpu_*): rows are
    rewritten by column name with blanks for the new fields, so appending a
    new-format row can never silently misalign against an old header.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.stat().st_size == 0:
        path.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8")
        return
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        if header == list(CSV_COLUMNS):
            return
        old_rows = list(reader)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in old_rows:
            writer.writerow({col: row.get(col, "") or "" for col in CSV_COLUMNS})


def append_csv_row(path: Path, row: dict[str, str]) -> None:
    # Parallel run_task.py processes share this file; the lock makes the
    # header-upgrade rewrite (and the append) safe against each other.
    with file_lock("runs-csv.lock"):
        ensure_csv(path)
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def run_success(
    success: dict[str, Any],
    fixture_root: Path | None = None,
    work_dir: Path | None = None,
) -> tuple[bool, str]:
    stype = success.get("type")
    if stype == "manual":
        return False, "manual scoring required"
    if stype == "file_contains":
        rel = Path(str(success.get("path", "")))
        pattern = str(success.get("pattern", ""))
        target = rel if rel.is_absolute() else ROOT / rel
        target = remap_fixture_path(target, fixture_root, work_dir)
        if not target.is_file():
            return False, f"missing file {rel}"
        text = target.read_text(encoding="utf-8")
        ok = pattern in text
        return ok, "" if ok else f"pattern not found in {rel}"
    if stype != "command":
        return False, f"unsupported success.type {stype!r}"

    cwd_rel = Path(str(success.get("cwd") or "."))
    cwd = cwd_rel if cwd_rel.is_absolute() else ROOT / cwd_rel
    cwd = remap_fixture_path(cwd, fixture_root, work_dir)
    command = str(success.get("command", "")).strip()
    if not command:
        return False, "empty success.command"
    expect_exit = success.get("expect_exit", 0)
    try:
        expect_exit = int(expect_exit)
    except (TypeError, ValueError):
        expect_exit = 0

    proc = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    ok = proc.returncode == expect_exit
    detail = ""
    if not ok:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = err[-1] if err else f"exit {proc.returncode}"
        detail = f"success command exit {proc.returncode}: {tail}"
    return ok, detail


def build_loop_cmd(args: argparse.Namespace, prompt: str, work_dir: Path | None = None) -> list[str]:
    raw = args.loop_bin or os.environ.get("LOOP_BIN") or "loop"
    cmd = shlex.split(raw)
    provider = args.provider or os.environ.get("LOOP_PROVIDER")
    model = args.model or os.environ.get("LOOP_MODEL")
    if provider:
        cmd.extend(["--provider", provider])
    if model:
        cmd.extend(["--model", model])
    # loop has a native --cwd flag (confirmed via `loop --help`) - set it
    # explicitly rather than relying only on the subprocess's own cwd, so
    # loop's own file resolution can't disagree with the OS cwd we set.
    if work_dir is not None:
        cmd.extend(["--cwd", str(work_dir)])
    cmd.extend(["--print", prompt])
    return cmd


def dsh_python(args: argparse.Namespace) -> str | None:
    """A Python that has `deepseek-harness-sdk` installed. Order: --dsh-python /
    DSH_PYTHON, this interpreter if it can import the SDK, then the sibling
    eval-bench checkout's .venv (where it's installed on this machine)."""
    explicit = args.dsh_python or os.environ.get("DSH_PYTHON")
    if explicit:
        return explicit
    try:
        import deepseek_harness  # noqa: F401

        return sys.executable
    except ImportError:
        pass
    sibling = ROOT.parent / "eval-bench" / ".venv" / "bin" / "python"
    return str(sibling) if sibling.is_file() else None


def build_dsh_cmd(args: argparse.Namespace, prompt: str) -> list[str]:
    # Runs through the SDK (scripts/dsh_sdk_run.py) rather than
    # `dsh --profile headless`, which has no provider/model flags. DSH_HOME
    # and the working directory reach the helper via env / subprocess cwd.
    py = dsh_python(args) or "python-with-deepseek-harness-sdk (set DSH_PYTHON)"
    cmd = [py, str(Path(__file__).resolve().parent / "dsh_sdk_run.py")]
    cmd.extend(["--provider", args.provider or "deepseek-official"])
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.extend(["--prompt", prompt])
    return cmd


def build_goose_cmd(args: argparse.Namespace, prompt: str) -> list[str]:
    raw = args.goose_bin or os.environ.get("GOOSE_BIN") or "goose"
    cmd = shlex.split(raw)
    cmd.append("run")
    if args.provider:
        cmd.extend(["--provider", args.provider])
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.extend(["-t", prompt, "-q"])
    return cmd


def build_pi_cmd(args: argparse.Namespace, prompt: str) -> list[str]:
    raw = args.pi_bin or os.environ.get("PI_BIN") or "pi"
    cmd = shlex.split(raw)
    # Flag order matches a verified-working invocation
    # (`pi --print "..." --provider ... --model ...`) rather than an
    # unverified reordering.
    cmd.extend(["--print", prompt])
    if args.provider:
        cmd.extend(["--provider", args.provider])
    if args.model:
        cmd.extend(["--model", args.model])
    pi_thinking = (getattr(args, "pi_thinking", "") or os.environ.get("PI_THINKING", "") or harness_thinking(args)).strip()
    if pi_thinking:
        cmd.extend(["--thinking", pi_thinking])
    # optional pi extension package (e.g. a LemonHarness checkout), loaded for this run only
    ext = os.environ.get("PI_EXTENSION", "").strip()
    if ext:
        cmd.extend(["-e", ext])
    return cmd


def opencode_cmd_prefix(args: argparse.Namespace) -> list[str]:
    return shlex.split(args.opencode_bin or os.environ.get("OPENCODE_BIN") or "opencode")


def build_opencode_cmd(args: argparse.Namespace, prompt: str) -> list[str]:
    cmd = opencode_cmd_prefix(args)
    # --standalone: private per-run server instead of the shared background
    # service, so the real work happens in this child process (meaningful
    # peak-RSS) and concurrent runs don't contend on one daemon.
    # --auto: auto-approve tool permissions, required for headless runs.
    cmd.extend(["run", "--standalone", "--auto", "--format", "json"])
    if args.model:
        # opencode takes one combined provider/model string
        model_id = f"{args.provider}/{args.model}" if args.provider else args.model
        if harness_thinking(args):
            # opencode provider/model#variant form; this model only offers low|high, so the
            # closest to "minimal" is "low"
            lvl = harness_thinking(args)
            model_id += f"#{'low' if lvl in ('minimal', 'none') else lvl}"
        cmd.extend(["--model", model_id])
    cmd.append(prompt)
    return cmd


def build_hermes_cmd(args: argparse.Namespace, prompt: str, hermes_home: Path, work_dir: Path | None = None) -> list[str]:
    raw = args.hermes_bin or os.environ.get("HERMES_BIN") or "hermes"
    cmd = shlex.split(raw)
    # -z: one-shot, final text only. --yolo: no approval prompts (headless).
    # --ignore-rules: skip memory / SOUL.md / AGENTS.md / skill injection, so a
    # run depends only on the task (Hermes is built to learn across sessions).
    # --usage-file: JSON report with tokens, api calls and cost.
    cmd.extend(["-z", prompt, "--yolo", "--ignore-rules", "--usage-file", str(hermes_home / "usage.json")])
    if args.provider:
        cmd.extend(["--provider", args.provider])
    if args.model:
        cmd.extend(["-m", args.model])
    if work_dir is not None:
        cmd.extend(["--in", str(work_dir)])
    if harness_thinking(args):
        cmd.extend(["--reasoning", harness_thinking(args)])
    return cmd


def build_kilo_cmd(args: argparse.Namespace, prompt: str, work_dir: Path | None = None) -> list[str]:
    raw = args.kilo_bin or os.environ.get("KILO_BIN") or "kilo"
    cmd = shlex.split(raw)
    # Kilo Code is an OpenCode fork. `run` starts its own local server per call;
    # --auto approves tool use for headless runs. Provider/model is one string.
    # NB: bare `kilo/<model>` is Kilo's own paid gateway; `openrouter/<model>`
    # is the route that uses our OpenRouter key like every other harness.
    cmd.extend(["run", "--auto", "--format", "json"])
    if args.model:
        cmd.extend(["--model", f"{args.provider}/{args.model}" if args.provider else args.model])
    if work_dir is not None:
        cmd.extend(["--dir", str(work_dir)])
    if harness_thinking(args):
        cmd.extend(["--variant", harness_thinking(args)])
    cmd.append(prompt)
    return cmd


def build_cline_cmd(args: argparse.Namespace, prompt: str, cline_home: Path, work_dir: Path | None = None) -> list[str]:
    raw = args.cline_bin or os.environ.get("CLINE_BIN") or "cline"
    cmd = shlex.split(raw)
    # --json: event stream. Auto-approve is already Cline's default. --data-dir /
    # --config isolate all state per run. The API key reaches it through the
    # environment (OPENROUTER_API_KEY) so it never appears in a process listing.
    cmd.extend(["--json", "--data-dir", str(cline_home / "data"), "--config", str(cline_home / "config")])
    if args.provider:
        cmd.extend(["-P", args.provider])
    if args.model:
        cmd.extend(["-m", args.model])
    if work_dir is not None:
        cmd.extend(["--cwd", str(work_dir)])
    if harness_thinking(args):
        lvl = harness_thinking(args)
        cmd.extend(["--thinking", "low" if lvl == "minimal" else lvl])  # cline scale: none|low|medium|high|xhigh
    cmd.append(prompt)
    return cmd


def harness_thinking(args: argparse.Namespace) -> str:
    """Requested reasoning level for this run (THINKING_LEVEL / --thinking), '' for the
    harness's own default. Each builder maps it onto that harness's own flag."""
    return (getattr(args, "thinking", "") or os.environ.get("THINKING_LEVEL", "")).strip()


def build_omp_cmd(args: argparse.Namespace, prompt: str, omp_home: Path, work_dir: Path | None = None) -> list[str]:
    raw = args.omp_bin or os.environ.get("OMP_BIN") or "omp"
    cmd = shlex.split(raw)
    # OMP (oh-my-pi) is a Pi-family agent needing the Bun runtime. --print is
    # one-shot, --auto-approve skips tool prompts, --no-title avoids a paid
    # background title call. Its session .jsonl has Pi's exact shape (verified
    # on a real run), so it lands in a per-run --session-dir and is read with
    # the Pi parser. The key reaches it through the environment.
    cmd.extend(["--print", "--auto-approve", "--no-title", "--session-dir", str(omp_home)])
    if work_dir is not None:
        cmd.extend(["--cwd", str(work_dir)])
    if args.model:
        cmd.extend(["--model", f"{args.provider}/{args.model}" if args.provider else args.model])
    if harness_thinking(args):
        cmd.extend(["--thinking", harness_thinking(args)])
    cmd.append(prompt)
    return cmd


def build_codex_cmd(args: argparse.Namespace, prompt: str, work_dir: Path | None = None) -> list[str]:
    raw = args.codex_bin or os.environ.get("CODEX_BIN") or "codex"
    cmd = shlex.split(raw)
    # exec = non-interactive. The sandbox/approval bypass is for headless use on
    # a throwaway copy; --ignore-user-config/--ignore-rules keep the run
    # independent of ~/.codex (CODEX_HOME is a fresh dir anyway).
    cmd.extend(["exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                "--ignore-user-config", "--ignore-rules"])
    if work_dir is not None:
        cmd.extend(["-C", str(work_dir)])
    if args.provider == "openrouter":
        # Codex has no built-in OpenRouter provider; declare one. The key comes
        # from the environment (env_key), never the command line. Note Codex
        # warns "model metadata not found" for models it doesn't know and falls
        # back to defaults - a real caveat for non-OpenAI models.
        for kv in ('model_provider="openrouter"',
                   'model_providers.openrouter.name="OpenRouter"',
                   'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
                   'model_providers.openrouter.env_key="OPENROUTER_API_KEY"'):
            cmd.extend(["-c", kv])
    if args.model:
        cmd.extend(["-m", args.model])
    if harness_thinking(args):
        cmd.extend(["-c", f'model_reasoning_effort="{harness_thinking(args)}"'])
    cmd.append(prompt)
    return cmd


TRANSPORT_MARKERS = ("Transport error", "error sending request", "error decoding response body", "sse error",
                     "ECONNRESET", "ETIMEDOUT", "socket hang up", "fetch failed", "Connection reset", "connection closed")


def _latest_mtime(paths: list[Path]) -> float:
    latest = 0.0
    for root in paths:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in ("node_modules", ".git", "target", ".verify")]
            for f in fns:
                try:
                    latest = max(latest, os.stat(os.path.join(dp, f)).st_mtime)
                except OSError:
                    pass
    return latest


def run_supervised(args, cmd, agent_cwd, run_env, timeout_s, watch, prompt, work_dir, stall_s, max_restarts):
    """Run the agent so that a hang or a dropped connection does not fail the task.

    The agent is watched for activity (any file changing in the project or the harness's own session folders, or new
    output). If it goes quiet for `stall_s`, or exits with a connection error, it is restarted in the *same* project
    with a prompt that says it is resuming, up to `max_restarts` times. Returns (completed process stand-in, notes,
    seconds lost to stalls). Raises subprocess.TimeoutExpired when the overall time limit is reached.
    """
    import signal
    from types import SimpleNamespace

    t_start = time.perf_counter()
    outs: list[str] = []
    errs: list[str] = []
    notes: list[str] = []
    lost = 0.0
    restarts = 0
    cur = cmd
    while True:
        fo, fe = tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")
        proc = subprocess.Popen(cur, cwd=agent_cwd, env=run_env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                text=True, start_new_session=True)
        last_m = _latest_mtime(watch)
        last_sz = 0
        last_act = time.time()
        stalled = False
        timed_out = False
        while proc.poll() is None:
            time.sleep(5)
            if time.perf_counter() - t_start > timeout_s:
                timed_out = True
                break
            m = _latest_mtime(watch)
            sz = fo.tell() + fe.tell()
            if m > last_m or sz != last_sz:
                last_m, last_sz, last_act = max(m, last_m), sz, time.time()
            elif time.time() - last_act > stall_s:
                stalled = True
                break
        if timed_out or stalled:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        for f, bucket in ((fo, outs), (fe, errs)):
            f.seek(0)
            bucket.append(f.read())
            f.close()
        if timed_out:
            raise subprocess.TimeoutExpired(cur, timeout_s, output="".join(outs), stderr="".join(errs))
        transport = proc.returncode not in (0, None) and any(m in errs[-1] or m in outs[-1] for m in TRANSPORT_MARKERS)
        if (stalled or transport) and restarts < max_restarts:
            restarts += 1
            if stalled:
                lost += stall_s
            notes.append(f"restart {restarts}: {'stalled ' + str(stall_s) + 's' if stalled else 'connection error'}")
            resume = ("Your earlier session was interrupted by a connection problem, so you are resuming. Everything you did so far is saved in "
                      "the project directory. Look at the project (README, docs/, src/, any build or test output) to see where you left off, then "
                      "carry on until the original task is finished. The original task was:\n\n" + prompt)
            cur = build_agent_cmd(args, resume, work_dir)
            continue
        break
    if restarts:
        notes.append(f"restarts={restarts}; stall_lost_s={int(lost)}")
    return SimpleNamespace(stdout="".join(outs), stderr="".join(errs), returncode=proc.returncode), notes, lost


def build_agent_cmd(args: argparse.Namespace, prompt: str, work_dir: Path | None = None) -> list[str]:
    if args.harness == "deepseek-harness":
        return build_dsh_cmd(args, prompt)
    if args.harness == "goose":
        return build_goose_cmd(args, prompt)
    if args.harness == "pi":
        return build_pi_cmd(args, prompt)
    if args.harness == "opencode":
        return build_opencode_cmd(args, prompt)
    if args.harness == "kilo":
        return build_kilo_cmd(args, prompt, work_dir)
    if args.harness == "codex":
        return build_codex_cmd(args, prompt, work_dir)
    return build_loop_cmd(args, prompt, work_dir)


def run_task(args: argparse.Namespace) -> int:
    task_path = resolve_task_path(args.task)
    data = parse_simple_yaml(task_path.read_text(encoding="utf-8"))
    task_id = str(data.get("id", task_path.stem))
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        raise SystemExit(f"{task_path}: empty prompt")

    repo = data.get("repo") if isinstance(data.get("repo"), dict) else {}
    success_spec = data.get("success") if isinstance(data.get("success"), dict) else {}
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}

    notes: list[str] = []
    if not args.no_reset:
        warn = reset_fixture(repo)
        if warn:
            notes.append(warn)

    # Isolate the agent's actual edits (and the grading check) to a per-run
    # copy of the fixture, not the shared fixtures/<name> directory in this
    # repo - required for two runs on the same fixture to not race on the
    # same files on disk, and a prerequisite for pointing loop's --cwd /
    # every harness's subprocess cwd at the fixture root the task prompts
    # assume (bare filenames like "services.html"), rather than this repo's
    # own root where those files don't exist.
    fixture_root: Path | None = None
    work_dir: Path | None = None
    if repo.get("type") == "fixture":
        rel = str(repo.get("path", "")).strip()
        if rel:
            candidate = Path(rel) if Path(rel).is_absolute() else ROOT / rel
            if candidate.is_dir():
                fixture_root = candidate.resolve()
                work_dir = make_work_dir(fixture_root, task_id)

    timeout_minutes = limits.get("timeout_minutes", 20)
    try:
        timeout_s = max(1, int(timeout_minutes)) * 60
    except (TypeError, ValueError):
        timeout_s = 20 * 60

    cmd = build_agent_cmd(args, prompt, work_dir)
    model_label = args.model or os.environ.get("LOOP_MODEL") or ""
    harness = args.harness
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    run_id = f"{date}-{task_id}-{now.strftime('%H%M%S')}"
    agent_cwd = work_dir if work_dir is not None else ROOT

    # loop, dsh, and pi each get a fresh, isolated per-run session-storage
    # directory, confirmed (via `loop config` / a real dsh-style run / a
    # real pi run respectively) to actually relocate where the binary
    # writes its session db/file - this is what makes two concurrent runs
    # of the same harness not misattribute each other's tokens/turns.
    run_env = os.environ.copy()
    # subprocess.run(cwd=...) changes the real cwd but leaves the inherited
    # $PWD stale, and some harnesses (confirmed for opencode: its bash tool's
    # `pwd` returned the stale value, and its edits landed in the real repo
    # fixture instead of the isolated copy) resolve the project directory from
    # $PWD. Keep it in sync with the directory we actually chose.
    run_env["PWD"] = str(agent_cwd)

    if harness_thinking(args) and harness not in ("loop", "pi"):
        if harness in ("goose", "deepseek-harness"):
            notes.append(f"thinking=default (no flag; requested {harness_thinking(args)})")
        else:
            notes.append(f"thinking={harness_thinking(args)}")

    loop_home: Path | None = None
    if harness == "loop":
        loop_home = Path(tempfile.mkdtemp(prefix=f"loop-home-{task_id}-"))
        run_env["LOOP_CODING_AGENT_SESSION_DIR"] = str(loop_home)
        # Loop's default thinking level is "off", which sends NO reasoning setting, so a
        # reasoning-by-default model (GLM 5.3 Flash on OpenRouter) keeps thinking at full
        # effort and OpenRouter refuses to disable it. An explicit level (e.g. "minimal")
        # is sent as reasoning.effort. Loop reads the level from settings.json, so give
        # this run its own agent dir: the user's real files are symlinked (auth etc.) and
        # only settings.json is a modified copy - the real settings are never touched.
        loop_thinking = (getattr(args, "loop_thinking", "") or os.environ.get("LOOP_THINKING", "") or harness_thinking(args)).strip()
        compact_trigger = os.environ.get("LOOP_COMPACT_TRIGGER", "").strip()
        otlp_ep = os.environ.get("LOOP_OTLP_ENDPOINT", "").strip()
        if loop_thinking or compact_trigger or otlp_ep:
            real_agent = Path(
                os.environ.get("LOOP_CODING_AGENT_DIR", "").strip() or Path.home() / ".loop" / "agent"
            )
            agent_cfg = loop_home / "agent-config"
            agent_cfg.mkdir()
            for name in ("auth.json", "models.json", "models-store.json", "trust.json"):
                if (real_agent / name).exists():
                    (agent_cfg / name).symlink_to(real_agent / name)
            try:
                settings = json.loads((real_agent / "settings.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                settings = {}
            if loop_thinking:
                settings["defaultThinkingLevel"] = loop_thinking
            if compact_trigger:
                comp = settings.setdefault("compaction", {})
                comp["enabled"] = True
                comp["triggerTokens"] = int(compact_trigger)
                comp["keepRecentTokens"] = int(os.environ.get("LOOP_COMPACT_KEEP", "4000"))
                # two-tier fork only: prune settings (ignored by builds without them)
                if os.environ.get("LOOP_PRUNE", "").strip() == "off":
                    comp["pruneEnabled"] = False
                if os.environ.get("LOOP_PRUNE_TRIGGER", "").strip():
                    comp["pruneTriggerTokens"] = int(os.environ["LOOP_PRUNE_TRIGGER"])
                if os.environ.get("LOOP_PRUNE_KEEP", "").strip():
                    comp["pruneKeepTokens"] = int(os.environ["LOOP_PRUNE_KEEP"])
                if os.environ.get("LOOP_PRUNE_MIN", "").strip():
                    comp["pruneMinReclaimTokens"] = int(os.environ["LOOP_PRUNE_MIN"])
                notes.append(f"compact_trigger={compact_trigger}")
            if otlp_ep:
                # this run's own settings: send Loop's built-in telemetry to a local OTLP collector
                settings["tracing"] = {"enabled": True, "backend": "otlp", "otlpEndpoint": otlp_ep}
                notes.append(f"otlp={otlp_ep}")
            (agent_cfg / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
            run_env["LOOP_CODING_AGENT_DIR"] = str(agent_cfg)
            if loop_thinking:
                notes.append(f"thinking={loop_thinking}")

    dsh_home: Path | None = None
    if harness == "deepseek-harness":
        dsh_home = Path(tempfile.mkdtemp(prefix=f"dsh-home-{task_id}-"))
        run_env["DSH_HOME"] = str(dsh_home)

    # goose shares one sessions.db across runs (no per-run home override that
    # doesn't also relocate its config/auth) - snapshot existing session ids
    # beforehand so scrape_goose_metrics can tell which one this run created,
    # and serialize the whole run-and-scrape step against other concurrent
    # goose runs via goose_run_lock() so two of them can't both claim the
    # same "newest session." Other harnesses are unaffected (nullcontext).
    goose_db_path: Path | None = None
    goose_known_sessions: set[str] = set()
    if harness == "goose":
        goose_cmd_prefix = shlex.split(args.goose_bin or os.environ.get("GOOSE_BIN") or "goose")
        goose_db_path = goose_sessions_db_path(goose_cmd_prefix)
        if goose_db_path is not None:
            goose_known_sessions = list_goose_session_ids(goose_db_path)
    run_lock = goose_run_lock() if harness == "goose" else contextlib.nullcontext()

    # pi's own docs name PI_CODING_AGENT_SESSION_DIR as the override with
    # highest precedence over its default ~/.pi/agent/sessions/ - same
    # per-run isolation trick as DSH_HOME, confirmed directly to produce a
    # findable .jsonl file even on a failed/errored run.
    pi_home: Path | None = None
    if harness == "pi":
        pi_home = Path(tempfile.mkdtemp(prefix=f"pi-home-{task_id}-"))
        run_env["PI_CODING_AGENT_SESSION_DIR"] = str(pi_home)
        pi_thinking = (getattr(args, "pi_thinking", "") or os.environ.get("PI_THINKING", "") or harness_thinking(args)).strip()
        if pi_thinking:
            notes.append(f"thinking={pi_thinking}")

    # Hermes keeps persistent memory, learned skills and a session db under
    # HERMES_HOME, so each run gets a brand-new one (verified: all state stays
    # inside it) - no carry-over between tasks, and parallel-safe.
    hermes_home: Path | None = None
    if harness == "hermes":
        hermes_home = Path(tempfile.mkdtemp(prefix=f"hermes-home-{task_id}-"))
        run_env["HERMES_HOME"] = str(hermes_home)
        # Hermes's shell tool does NOT start in the process cwd or --in dir: a
        # probe printed $HOME. It starts where TERMINAL_CWD says (verified), so
        # without this the agent hunts for the project and edited the real repo
        # fixture instead of the isolated copy.
        run_env["TERMINAL_CWD"] = str(agent_cwd)
        cmd = build_hermes_cmd(args, prompt, hermes_home, work_dir)

    # Cline keeps sessions, settings and a db under its data/config dirs; give
    # each run its own so nothing carries over (verified: state stays inside).
    cline_home: Path | None = None
    if harness == "cline":
        cline_home = Path(tempfile.mkdtemp(prefix=f"cline-home-{task_id}-"))
        cmd = build_cline_cmd(args, prompt, cline_home, work_dir)

    # Codex keeps sessions, memories and sqlite state under CODEX_HOME (verified
    # it contains a memories db), so each run gets a fresh one.
    codex_home: Path | None = None
    if harness == "codex":
        codex_home = Path(tempfile.mkdtemp(prefix=f"codex-home-{task_id}-"))
        run_env["CODEX_HOME"] = str(codex_home)

    omp_home: Path | None = None
    if harness == "omp":
        omp_home = Path(tempfile.mkdtemp(prefix=f"omp-home-{task_id}-"))
        cmd = build_omp_cmd(args, prompt, omp_home, work_dir)

    metrics: dict[str, Any] | None = None
    wall_ms = ""
    peak_rss_kb: int | None = None
    cpu_user_s: float | None = None
    cpu_sys_s: float | None = None
    started_ms = int(time.time() * 1000) - 1000
    try:
        with run_lock:
            ru_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            rss_before = ru_before.ru_maxrss
            t0 = time.perf_counter()
            try:
                resilient = os.environ.get("RESILIENT", "").strip() == "1" or str(limits.get("resilient", "")).lower() == "true"
                if resilient:
                    watch = [p for p in (work_dir, loop_home, pi_home, hermes_home, cline_home, codex_home, omp_home, dsh_home) if p is not None and Path(p).exists()]
                    proc, sup_notes, stall_lost = run_supervised(
                        args, cmd, agent_cwd, run_env, timeout_s, watch, prompt, work_dir,
                        int(limits.get("stall_seconds", os.environ.get("STALL_SECONDS", 600))), int(limits.get("max_restarts", 8)))
                    notes.extend(sup_notes)
                else:
                    proc = subprocess.run(
                        cmd,
                        cwd=agent_cwd,
                        env=run_env,
                        # Closed stdin: pi (print mode) prepends piped stdin to the
                        # prompt and waits for EOF, so an open-but-silent stdin made
                        # it hang indefinitely (reproduced: 80s+ with no output,
                        # 17s with stdin=/dev/null). No harness here wants input.
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=timeout_s,
                    )
                wall_ms = str(int((time.perf_counter() - t0) * 1000))
                ru_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                rss_after = ru_after.ru_maxrss
                # CPU seconds the child tree burned (user + kernel), a delta
                # of cumulative counters, so unlike ru_maxrss it is exact even
                # if something larger ran earlier in this process.
                cpu_user_s = max(0.0, ru_after.ru_utime - ru_before.ru_utime)
                cpu_sys_s = max(0.0, ru_after.ru_stime - ru_before.ru_stime)
                # ru_maxrss is a running peak across every child this process has
                # ever spawned, not reset per-call - the delta isolates this call's
                # contribution as long as no larger child ran concurrently. macOS
                # reports bytes; Linux reports KB - normalize to KB either way.
                delta = max(0, rss_after - rss_before)
                peak_rss_kb = delta // 1024 if sys.platform == "darwin" else delta
                if harness == "deepseek-harness":
                    metrics = scrape_dsh_metrics(dsh_home) if dsh_home else None
                elif harness == "goose":
                    metrics = scrape_goose_metrics(goose_db_path, goose_known_sessions) if goose_db_path else None
                elif harness == "pi":
                    metrics = scrape_pi_metrics(pi_home) if pi_home else None
                elif harness == "codex":
                    metrics = scrape_codex_metrics(codex_home) if codex_home else None
                elif harness == "omp":
                    metrics = scrape_pi_metrics(omp_home) if omp_home else None
                elif harness == "cline":
                    metrics = metrics_from_cline_events(proc.stdout or "")
                elif harness == "kilo":
                    metrics = metrics_from_kilo_events(proc.stdout or "")
                elif harness == "hermes":
                    metrics = scrape_hermes_metrics(hermes_home) if hermes_home else None
                elif harness == "opencode":
                    metrics = scrape_opencode_metrics(proc.stdout or "", opencode_cmd_prefix(args), run_env, agent_cwd)
                else:
                    metrics = scrape_session_metrics(started_ms, loop_home)
                if proc.stdout:
                    sys.stdout.write(proc.stdout)
                    if not proc.stdout.endswith("\n"):
                        sys.stdout.write("\n")
                if proc.stderr:
                    sys.stderr.write(proc.stderr)
                    if not proc.stderr.endswith("\n"):
                        sys.stderr.write("\n")
                if proc.returncode != 0:
                    notes.append(summarize_loop_stderr(proc.stderr, proc.returncode))
            except FileNotFoundError:
                wall_ms = str(int((time.perf_counter() - t0) * 1000))
                notes.append(f"{cmd[0]} binary not found")
            except subprocess.TimeoutExpired:
                wall_ms = str(int((time.perf_counter() - t0) * 1000))
                notes.append(f"timeout after {timeout_s}s")

        passed, success_note = run_success(success_spec, fixture_root, work_dir)
        if success_note:
            notes.append(success_note)
        if work_dir is not None and (Path(work_dir) / ".progress.json").is_file():
            try:  # staged tasks: how far the agent got, even when the final check fails
                notes.append("stages_unlocked=%s" % json.loads((Path(work_dir) / ".progress.json").read_text())["unlocked"])
            except (OSError, ValueError, KeyError):
                pass
    finally:
        if loop_home is not None:
            shutil.rmtree(loop_home, ignore_errors=True)
        if dsh_home is not None:
            # DSH_KEEP_SESSIONS=<dir>: keep a copy of this run's session log (it carries a
            # timestamp per entry, so model-wait vs tool time can be measured afterwards).
            keep = os.environ.get("DSH_KEEP_SESSIONS", "").strip()
            if keep:
                try:
                    Path(keep).mkdir(parents=True, exist_ok=True)
                    for i, src in enumerate(sorted(dsh_home.rglob("session.v*.jsonl"))):
                        shutil.copy2(src, Path(keep) / f"{task_id}-{int(time.time())}-{i}.jsonl")
                except OSError:
                    pass
            shutil.rmtree(dsh_home, ignore_errors=True)
        if pi_home is not None:
            shutil.rmtree(pi_home, ignore_errors=True)
        if hermes_home is not None:
            shutil.rmtree(hermes_home, ignore_errors=True)
        if cline_home is not None:
            shutil.rmtree(cline_home, ignore_errors=True)
        if omp_home is not None:
            shutil.rmtree(omp_home, ignore_errors=True)
        if codex_home is not None:
            shutil.rmtree(codex_home, ignore_errors=True)
        quality_dir = os.environ.get("QUALITY_DIR", "").strip()
        if quality_dir and work_dir is not None and fixture_root is not None:
            try:
                save_quality_snapshot(
                    Path(quality_dir), os.environ.get("QUALITY_TAG", "").strip() or harness, task_id,
                    fixture_root, work_dir, harness, getattr(args, "model", "") or "", locals().get("passed"),
                )
            except Exception as exc:  # the snapshot must never fail a benchmark run
                print(f"warning: quality snapshot failed: {exc}", file=sys.stderr)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

    session_id = ""
    if metrics:
        session_id = str(metrics.get("session_id") or "")
        if session_id:
            notes.append(f"session={session_id}")
        if not model_label:
            model_label = str(metrics.get("model") or "")

    row = {
        "run_id": run_id,
        "date": date,
        "harness": harness,
        "model": model_label,
        "task_id": task_id,
        "repo_sha": git_sha(),
        "success": "true" if passed else "false",
        "input_tokens": csv_int(metrics.get("input_tokens") if metrics else None),
        "output_tokens": csv_int(metrics.get("output_tokens") if metrics else None),
        "total_tokens": csv_int(metrics.get("total_tokens") if metrics else None),
        "t_first_token_ms": "",
        "t_last_token_ms": "",
        "wall_clock_ms": wall_ms,
        "peak_rss_kb": csv_int(peak_rss_kb),
        "cpu_user_s": csv_float(cpu_user_s),
        "cpu_sys_s": csv_float(cpu_sys_s),
        "assistant_turns": csv_int(metrics.get("assistant_turns") if metrics else None),
        "tool_calls": csv_int(metrics.get("tool_calls") if metrics else None),
        "retries": "0",
        "notes": "; ".join(n for n in notes if n),
    }
    append_csv_row(Path(args.output), row)
    print(
        f"logged {run_id} success={row['success']} wall_clock_ms={wall_ms} "
        f"tokens={row['total_tokens'] or '—'} turns={row['assistant_turns'] or '—'} "
        f"peak_rss_kb={row['peak_rss_kb'] or '—'} cpu_s={row['cpu_user_s'] or '—'}u/{row['cpu_sys_s'] or '—'}s -> {args.output}",
        file=sys.stderr,
    )
    return 0 if passed else 1


def self_test() -> int:
    payload = json.dumps(
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "model": "qwen3-8-27b",
                "usage": {
                    "input": 7680,
                    "output": 480,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 8160,
                },
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "toolCall", "id": "1", "name": "bash", "arguments": {}},
                ],
            },
        }
    )
    parsed = metrics_from_entry_payloads([payload])
    assert parsed["input_tokens"] == 7680
    assert parsed["output_tokens"] == 480
    assert parsed["total_tokens"] == 8160
    assert parsed["assistant_turns"] == 1
    assert parsed["tool_calls"] == 1
    assert parsed["model"] == "qwen3-8-27b"

    dsh_real = [
        {"type": "session"},
        {"type": "assistant/message", "data": {"usage": {"inputTokens": 21, "outputTokens": 125, "totalTokens": 1234, "cacheReadTokens": 1088}}},
        {"type": "tool/call", "data": {"name": "str_replace_editor"}},
        {"type": "tool/result", "data": {}},
        {"type": "assistant/message", "data": {"usage": {"inputTokens": 1263, "outputTokens": 51, "totalTokens": 1314}}},
        {"type": "tool/call", "data": {"name": "bash"}},
    ]
    dsh_real_parsed = metrics_from_dsh_usage_entries(dsh_real)
    assert dsh_real_parsed["input_tokens"] == 21 + 1088 + 1263
    assert dsh_real_parsed["output_tokens"] == 125 + 51
    assert dsh_real_parsed["total_tokens"] == 1234 + 1314
    assert dsh_real_parsed["assistant_turns"] == 2
    assert dsh_real_parsed["tool_calls"] == 2

    dsh_entries = [
        {"type": "usage", "usage": {"inputTokens": 670, "outputTokens": 39, "totalTokens": 789, "cacheReadTokens": 80}},
        {"type": "usage", "usage": {"inputTokens": 5761, "outputTokens": 46, "totalTokens": 5887, "cacheReadTokens": 80}},
    ]
    dsh_parsed = metrics_from_dsh_usage_entries(dsh_entries)
    # cacheReadTokens fold into input (this fixture's own totalTokens 789 ==
    # 670 + 39 + 80 confirms inputTokens excludes them).
    assert dsh_parsed["input_tokens"] == 670 + 80 + 5761 + 80
    assert dsh_parsed["output_tokens"] == 39 + 46
    assert dsh_parsed["total_tokens"] == 670 + 80 + 5761 + 80 + 39 + 46
    assert dsh_parsed["assistant_turns"] == 2

    # (accumulated_input, accumulated_output, accumulated_cache_read,
    #  accumulated_cache_write, accumulated_cost) - one goose `sessions` row.
    goose_row = (12000, 3400, 900, 0, 0.0183)
    goose_parsed = metrics_from_goose_session_row(goose_row, assistant_turns=6, tool_calls=4)
    assert goose_parsed["tool_calls"] == 4
    assert goose_parsed["input_tokens"] == 12000 + 900
    assert goose_parsed["output_tokens"] == 3400
    assert goose_parsed["total_tokens"] == 12000 + 900 + 3400
    assert goose_parsed["assistant_turns"] == 6
    assert goose_parsed["cost_usd"] == 0.0183

    # Shape matches a real pi session.jsonl captured live (see
    # metrics_from_pi_entries' docstring): session header + model_change +
    # system message are present in a real file but ignored here since they
    # aren't type "message" with an assistant role; only the two
    # user/assistant pair's usage should be summed.
    pi_entries = [
        {"type": "session", "version": 3, "id": "x"},
        {"type": "model_change", "id": "a", "parentId": None, "provider": "openrouter", "modelId": "m"},
        {"type": "message", "id": "b", "parentId": "a", "message": {"role": "user", "content": "hi"}},
        {
            "type": "message",
            "id": "c",
            "parentId": "b",
            "message": {
                "role": "assistant",
                "model": "deepseek/deepseek-v4-flash",
                "content": [{"type": "text", "text": "x"}, {"type": "toolCall", "id": "1"}, {"type": "toolCall", "id": "2"}],
                "usage": {
                    "input": 500,
                    "output": 120,
                    "cacheRead": 50,
                    "cacheWrite": 0,
                    "totalTokens": 670,
                    "cost": {"input": 0.001, "output": 0.0005, "cacheRead": 0, "cacheWrite": 0, "total": 0.0015},
                },
            },
        },
    ]
    pi_parsed = metrics_from_pi_entries(pi_entries)
    assert pi_parsed["input_tokens"] == 500 + 50
    assert pi_parsed["output_tokens"] == 120
    assert pi_parsed["total_tokens"] == 500 + 50 + 120
    assert pi_parsed["assistant_turns"] == 1
    assert pi_parsed["tool_calls"] == 2
    assert pi_parsed["model"] == "deepseek/deepseek-v4-flash"
    assert pi_parsed["cost_usd"] == 0.0015

    # Shape from a real `opencode session export` (v2.0.20).
    oc_export = {
        "info": {
            "id": "ses_x",
            "model": {"id": "z-ai/glm-5.3-flash", "providerID": "openrouter"},
            "cost": 0.0009,
            "tokens": {"input": 6221, "output": 7, "reasoning": 71, "cache": {"read": 100, "write": 0}},
            "outcome": "succeeded",
        },
        "messages": [
            {"type": "user"},
            {"type": "assistant", "content": [{"type": "reasoning"}, {"type": "tool"}, {"type": "tool"}]},
            {"type": "assistant", "content": [{"type": "text"}]},
            {"type": "idle"},
        ],
    }
    oc_parsed = metrics_from_opencode_export(oc_export)
    assert oc_parsed["input_tokens"] == 6221 + 100
    assert oc_parsed["output_tokens"] == 7 + 71
    assert oc_parsed["total_tokens"] == 6221 + 100 + 7 + 71
    assert oc_parsed["assistant_turns"] == 2
    assert oc_parsed["tool_calls"] == 2
    assert oc_parsed["model"] == "z-ai/glm-5.3-flash"
    assert oc_parsed["cost_usd"] == 0.0009
    assert opencode_session_id_from_stdout('noise\n{"type":"text","sessionID":"ses_abc"}\n') == "ses_abc"
    assert opencode_session_id_from_stdout("no json here") is None

    hermes_usage = {
        "estimated_cost_usd": 0.0034525, "input_tokens": 22880, "output_tokens": 41, "cache_read_tokens": 100,
        "cache_write_tokens": 0, "api_calls": 2, "model": "z-ai/glm-5.3-flash", "session_id": "s1",
        "auxiliary": {"input_tokens": 246, "output_tokens": 11, "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated_cost_usd": 0},
    }
    hp = metrics_from_hermes(hermes_usage, tool_calls=3)
    assert hp["input_tokens"] == 22880 + 100 + 246
    assert hp["output_tokens"] == 41 + 11
    assert hp["total_tokens"] == 22880 + 100 + 246 + 41 + 11
    assert hp["assistant_turns"] == 2 and hp["tool_calls"] == 3 and hp["cost_usd"] == 0.0034525

    kilo_stdout = "\n".join([
        '{"type":"step_start","sessionID":"ses_k"}',
        '{"type":"tool_use","sessionID":"ses_k"}',
        '{"type":"step_finish","sessionID":"ses_k","part":{"cost":0.0004,"tokens":{"total":15316,"input":15296,"output":20,"reasoning":5,"cache":{"read":100,"write":0}}}}',
        '{"type":"text","sessionID":"ses_k"}',
        '{"type":"step_finish","sessionID":"ses_k","part":{"cost":0.0002,"tokens":{"input":500,"output":30,"reasoning":0,"cache":{"read":0,"write":0}}}}',
        "not json",
    ])
    km = metrics_from_kilo_events(kilo_stdout)
    assert km["input_tokens"] == 15296 + 100 + 500
    assert km["output_tokens"] == 20 + 5 + 30
    assert km["assistant_turns"] == 2 and km["tool_calls"] == 1 and km["session_id"] == "ses_k"
    assert abs(km["cost_usd"] - 0.0006) < 1e-9
    assert metrics_from_kilo_events("no events here") is None

    cline_stdout = "\n".join([
        '{"type":"hook_event","hookEventName":"agent_start"}',
        '{"type":"hook_event","hookEventName":"tool_call"}',
        '{"type":"hook_event","hookEventName":"tool_result"}',
        '{"type":"hook_event","hookEventName":"tool_call"}',
        '{"type":"run_result","iterations":2,"aggregateUsage":{"inputTokens":10230,"outputTokens":18,"cacheReadTokens":5056,"totalCost":0.00096},"model":{"id":"z-ai/glm-5.3-flash"}}',
    ])
    cm = metrics_from_cline_events(cline_stdout)
    assert cm["input_tokens"] == 10230 and cm["output_tokens"] == 18 and cm["total_tokens"] == 10248
    assert cm["assistant_turns"] == 2 and cm["tool_calls"] == 2 and cm["model"] == "z-ai/glm-5.3-flash"
    assert metrics_from_cline_events('{"type":"agent_event"}') is None

    codex_lines = [
        '{"type":"event_msg","payload":{"type":"task_started"}}',
        '{"type":"response_item","payload":{"type":"function_call","name":"shell"}}',
        '{"type":"event_msg","payload":{"type":"token_count","info":null}}',
        '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":8809,"cached_input_tokens":0,"output_tokens":67,"reasoning_output_tokens":47}}}}',
        '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":8879,"cached_input_tokens":0,"output_tokens":3,"reasoning_output_tokens":0}}}}',
    ]
    cx = metrics_from_codex_rollout(codex_lines)
    assert cx["input_tokens"] == 17688 and cx["output_tokens"] == 70 and cx["total_tokens"] == 17758
    assert cx["assistant_turns"] == 2 and cx["tool_calls"] == 1
    assert metrics_from_codex_rollout(["not json"]) is None

    print("OK: run_task self-test")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="Task id (WEB-BUG-01) or YAML path")
    parser.add_argument("--loop-bin", default="", help="Loop command prefix (default: LOOP_BIN or loop)")
    parser.add_argument("--loop-thinking", default="", help="Loop thinking level for this run: minimal|low|medium|high (default: LOOP_THINKING, else Loop's own setting)")
    parser.add_argument(
        "--dsh-python",
        default="",
        help="Python with deepseek-harness-sdk installed (default: DSH_PYTHON, this interpreter, or ../eval-bench/.venv)",
    )
    parser.add_argument("--goose-bin", default="", help="goose command prefix (default: GOOSE_BIN or goose)")
    parser.add_argument("--pi-bin", default="", help="pi command prefix (default: PI_BIN or pi)")
    parser.add_argument("--thinking", default="", help="Reasoning level for every harness that supports one (default: THINKING_LEVEL env, else each harness's own default)")
    parser.add_argument("--pi-thinking", default="", help="Pi --thinking level: off|minimal|low|medium|high (default: PI_THINKING, else Pi's own setting)")
    parser.add_argument("--codex-bin", default="", help="codex command prefix (default: CODEX_BIN or codex)")
    parser.add_argument("--omp-bin", default="", help="omp command prefix (default: OMP_BIN or omp)")
    parser.add_argument("--cline-bin", default="", help="cline command prefix (default: CLINE_BIN or cline)")
    parser.add_argument("--kilo-bin", default="", help="kilo command prefix (default: KILO_BIN or kilo)")
    parser.add_argument("--hermes-bin", default="", help="hermes command prefix (default: HERMES_BIN or hermes)")
    parser.add_argument("--opencode-bin", default="", help="opencode command prefix (default: OPENCODE_BIN or opencode)")
    parser.add_argument("--provider", default="", help="Forwarded to loop/goose/pi --provider (opencode: prefixed onto --model)")
    parser.add_argument(
        "--model",
        default="",
        help="Forwarded to loop/goose/pi/opencode/deepseek-harness --model",
    )
    parser.add_argument(
        "--harness",
        default="loop",
        choices=HARNESS_CHOICES,
        help="Which harness actually runs the task (default: loop)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_CSV),
        help="CSV path (default: runs/runs.csv)",
    )
    parser.add_argument("--no-reset", action="store_true", help="Do not git checkout the fixture")
    parser.add_argument("--self-test", action="store_true", help="Run metrics scrape checks and exit")
    args = parser.parse_args()

    if args.self_test:
        raise SystemExit(self_test())
    if not args.task:
        parser.error("task id or YAML path is required")
    raise SystemExit(run_task(args))


if __name__ == "__main__":
    main()
