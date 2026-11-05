"""Harness adapters for the nine benchmarked harnesses.

Reused from scripts/run_task.py (command builders + usage scrapers). NOT reused:
its ru_maxrss peak memory (invalid: it is a high-water mark of all children of the
python process) and its writable in-process success check (replaced by bench.verification
in a separate clean pod), nor its stall-restart supervisor (the benchmark forbids restarts).

What each adapter adds is ONLY the endpoint/model wiring (config files + env), driven by
config ``model.*``. Most configs reference the API-key env-var NAME. Cline 3.0.74 requires
its provider settings file to carry the key; that file is mode 0600 inside the private
per-trial home, excluded from artifacts, redacted, and deleted after the run.

UNRESOLVED UPSTREAM COMPATIBILITY: the custom-provider config shapes below were written
from each CLI's public documentation / the existing run_task usage and have NOT been
exercised against the Soket endpoint (it was returning HTTP 502). Every adapter carries
``compat`` metadata and ``bench qualify --stage protocol`` must pass per harness before a
scored run is allowed. No harness is silently substituted for another.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import HARNESSES
from .paths import ensure_scripts_importable, SCRIPTS_DIR

ensure_scripts_importable()
import run_task as rt  # noqa: E402  (original adapters)

BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "NODE_OPTIONS_SAFE")


@dataclass
class Ctx:
    workdir: Path          # agent project directory (cwd)
    home: Path             # per-trial harness state dir (fresh, private)
    prompt: str
    base_url: str
    model: str
    provider_id: str
    api_key_env: str
    loop_commit: str = ""
    started_ms: int = 0


@dataclass
class Invocation:
    cmd: list[str]
    env: dict[str, str]
    scrape: Callable[[str, str], dict[str, Any] | None]   # (stdout, stderr) -> metrics
    notes: list[str] = field(default_factory=list)


COMPAT: dict[str, dict[str, str]] = {
    h: {"status": "unverified", "notes": ""} for h in HARNESSES
}
COMPAT["loop"]["notes"] = "models.json/settings.json written to the schema read from the pinned commit's runtime.rs + settings.rs; still unexercised against the live endpoint"
COMPAT["pi"]["notes"] = "models.json openai-completions provider with env-var apiKey"
COMPAT["goose"]["notes"] = "1.57.0 custom_providers/<id>.json schema; base_url is full /chat/completions URL; goose info locates the isolated sessions DB"
COMPAT["opencode"]["notes"] = "1.19.5 CLI shape from official packages/opencode/src/cli/cmd/run.ts; opencode.json permission allow; step_finish usage events"
COMPAT["hermes"]["notes"] = "0.22.7 config.yaml custom model block + OPENAI_API_KEY; upstream removed OPENAI_BASE_URL as config"
COMPAT["cline"]["notes"] = "3.0.74 isolated providers.json openai-chat/openai-compatible settings; per-trial 0600 API key file"
COMPAT["kilo"]["notes"] = "7.8.18 kilo.json config uses @ai-sdk/openai-compatible, options.baseURL, env array, static model limits"
COMPAT["omp"]["notes"] = "18.16.0 HOME/.omp/agent/models.yml custom openai-completions provider with apiKey env-name lookup"
COMPAT["deepseek-harness"]["notes"] = "uses scripts/dsh_sdk_run.py extended with --provider custom (--base-url/--api-key-env)"


def _ns(**kw: Any) -> argparse.Namespace:
    base = {k: "" for k in ("loop_bin", "loop_thinking", "dsh_python", "goose_bin", "pi_bin", "thinking", "pi_thinking",
                            "codex_bin", "omp_bin", "cline_bin", "kilo_bin", "hermes_bin", "opencode_bin",
                            "provider", "model")}
    base.update(kw)
    return argparse.Namespace(**base)


def base_env(ctx: Ctx, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for the agent: no inherited worker env, only the key var it needs."""
    env = {k: os.environ[k] for k in BASE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update({"HOME": str(ctx.home), "PWD": str(ctx.workdir), "USER": "agent", "CI": "1",
                "XDG_CONFIG_HOME": str(ctx.home / ".config"), "XDG_DATA_HOME": str(ctx.home / ".local/share"),
                "XDG_CACHE_HOME": str(ctx.home / ".cache"), "CARGO_HOME": str(ctx.home / ".cargo-home"),
                "npm_config_cache": str(ctx.home / ".npm"), "TMPDIR": str(ctx.home / "tmp"),
                "NO_COLOR": "1"})
    # image-preloaded, locked dependencies (no per-trial npm install / network differences)
    env["NODE_PATH"] = os.environ.get("BENCH_NODE_PATH", "/opt/bench-deps/node_modules")
    for k in ("RUSTUP_HOME", "PLAYWRIGHT_BROWSERS_PATH"):
        if k in os.environ:
            env[k] = os.environ[k]
    if ctx.api_key_env in os.environ:
        env[ctx.api_key_env] = os.environ[ctx.api_key_env]
    if extra:
        env.update(extra)
    return env


def _mk(ctx: Ctx, *dirs: str) -> None:
    for d in ("tmp", ".config", ".cache", ".npm", *dirs):
        (ctx.home / d).mkdir(parents=True, exist_ok=True)


def pi_models_json(ctx: Ctx) -> dict:
    return {"providers": {ctx.provider_id: {
        "baseUrl": ctx.base_url, "api": "openai-completions", "apiKey": ctx.api_key_env,
        "models": [{"id": ctx.model, "name": ctx.model}]}}}


def opencode_json(ctx: Ctx) -> dict:
    return {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
            "provider": {ctx.provider_id: {
                "npm": "@ai-sdk/openai-compatible", "name": ctx.provider_id,
                "options": {"baseURL": ctx.base_url, "apiKey": "{env:%s}" % ctx.api_key_env},
                "models": {ctx.model: {"name": ctx.model}}}}}


def kilo_json(ctx: Ctx) -> dict:
    """Kilo's custom-provider schema is OpenCode-like but uses its own `env` key list.

    The official Kilo CLI docs recommend declaring context and output limits so its
    context manager can plan requests; API keys are resolved from the named env var.
    """
    return {"$schema": "https://app.kilo.ai/config.json", "autoupdate": False,
            "provider": {ctx.provider_id: {
                "npm": "@ai-sdk/openai-compatible", "name": ctx.provider_id,
                "env": [ctx.api_key_env], "options": {"baseURL": ctx.base_url},
                "models": {ctx.model: {"name": ctx.model,
                                        "limit": {"context": 128000, "output": 16384}}}}}}


# ----------------------------------------------------------------------------- builders
def loop_models_json(ctx: Ctx) -> dict:
    """Loop's models.json (crates/loop-app-core/src/runtime.rs load_custom_models_json at the pinned
    commit): `providers` is an ARRAY; apiKeyEnv is a LIST of env var names; models are ids or objects."""
    return {"providers": [{
        "id": ctx.provider_id, "name": ctx.provider_id, "baseUrl": ctx.base_url,
        "apiKeyEnv": [ctx.api_key_env],
        "models": [{"id": ctx.model, "name": ctx.model}]}]}


def loop_settings_json(ctx: Ctx) -> dict:
    """Unattended run: trust every project, auto-allow write/edit/bash, no sandbox runtime
    (the pod itself is the sandbox). `full-access` is only honoured through the
    --permission-mode CLI flag (see build_loop), never from settings."""
    return {"defaultProvider": ctx.provider_id, "defaultModel": ctx.model,
            "defaultProjectTrust": "always", "permissionMode": "accept-edits",
            "toolPermissions": {"write": "allow", "edit": "allow", "bash": "allow"},
            "sandbox": {"mode": "off"},
            "providers": [{"id": ctx.provider_id, "name": ctx.provider_id, "baseUrl": ctx.base_url}]}


def build_loop(ctx: Ctx) -> Invocation:
    _mk(ctx, "agent", "sessions")
    (ctx.home / "agent" / "models.json").write_text(json.dumps(loop_models_json(ctx), indent=2))
    (ctx.home / "agent" / "settings.json").write_text(json.dumps(loop_settings_json(ctx), indent=2))
    env = base_env(ctx, {"LOOP_CODING_AGENT_DIR": str(ctx.home / "agent"), "LOOP_CODING_AGENT_SESSION_DIR": str(ctx.home / "sessions")})
    cmd = rt.build_loop_cmd(_ns(provider=ctx.provider_id, model=ctx.model), ctx.prompt, ctx.workdir)
    cmd[1:1] = ["--permission-mode", "full-access"]   # global flag, precedes --print
    sdir = ctx.home / "sessions"
    return Invocation(cmd, env, lambda out, err: rt.scrape_session_metrics(ctx.started_ms, sdir),
                      ["models.json schema checked against pinned loop commit runtime.rs"])


def build_pi(ctx: Ctx) -> Invocation:
    _mk(ctx, "agent", "sessions")
    (ctx.home / "agent" / "models.json").write_text(json.dumps(pi_models_json(ctx), indent=2))
    env = base_env(ctx, {"PI_CODING_AGENT_DIR": str(ctx.home / "agent"), "PI_CODING_AGENT_SESSION_DIR": str(ctx.home / "sessions")})
    cmd = rt.build_pi_cmd(_ns(provider=ctx.provider_id, model=ctx.model), ctx.prompt)
    sdir = ctx.home / "sessions"
    return Invocation(cmd, env, lambda out, err: rt.scrape_pi_metrics(sdir))


def build_goose(ctx: Ctx) -> Invocation:
    """goose 1.57.0 (aaif-goose/goose). A custom provider's base_url is the FULL completions URL
    (documentation/docs/getting-started/providers.md), unlike the shared /v1 base_url, so
    /chat/completions is appended here only."""
    _mk(ctx, "goose-root/config/custom_providers")
    full_url = ctx.base_url.rstrip("/") + "/chat/completions"
    pj = {"name": ctx.provider_id, "engine": "openai", "display_name": ctx.provider_id, "description": "bench endpoint",
          "api_key_env": ctx.api_key_env, "base_url": full_url, "models": [{"name": ctx.model, "context_limit": 128000}],
          "supports_streaming": True, "requires_auth": True}
    (ctx.home / "goose-root/config/custom_providers" / f"{ctx.provider_id}.json").write_text(json.dumps(pj, indent=2))
    env = base_env(ctx, {"GOOSE_PATH_ROOT": str(ctx.home / "goose-root"), "GOOSE_PROVIDER": ctx.provider_id,
                         "GOOSE_MODEL": ctx.model, "GOOSE_DISABLE_KEYRING": "1"})
    cmd = rt.build_goose_cmd(_ns(provider=ctx.provider_id, model=ctx.model), ctx.prompt)

    def scrape(out: str, err: str):
        db = rt.goose_sessions_db_path(["goose"]) or (ctx.home / "goose-root/data/sessions/sessions.db")
        return rt.scrape_goose_metrics(Path(db), set())   # fresh GOOSE_PATH_ROOT per trial: no pre-existing sessions
    return Invocation(cmd, env, scrape, ["goose info consulted for the sessions db path", "base_url gets /chat/completions"])


def build_opencode(ctx: Ctx) -> Invocation:
    """opencode-ai 1.19.5: `run` options are model/format/dir/variant (no --standalone/--auto, which the
    old run_task builder passes). Permissions are allowed through opencode.json instead; usage comes from the
    JSON `step_finish` events on stdout (no `session export`)."""
    _mk(ctx)
    cfg = ctx.home / "opencode.json"
    conf = opencode_json(ctx)
    conf["permission"] = {"edit": "allow", "bash": "allow", "webfetch": "allow"}
    cfg.write_text(json.dumps(conf, indent=2))
    env = base_env(ctx, {"OPENCODE_CONFIG": str(cfg), "OPENCODE_DISABLE_AUTOUPDATE": "1"})
    cmd = ["opencode", "run", "--format", "json", "--dir", str(ctx.workdir),
           "--model", f"{ctx.provider_id}/{ctx.model}", ctx.prompt]
    return Invocation(cmd, env, lambda out, err: rt.metrics_from_kilo_events(out),
                      ["argv built directly (not run_task.build_opencode_cmd)",
                       "usage parsed from step_finish events; event shape assumed equal to the kilo/opencode fork's"])


def build_hermes(ctx: Ctx) -> Invocation:
    """hermes-agent 0.22.7 (NousResearch): HERMES_HOME/config.yaml `model:{default,provider:custom,base_url}`;
    the key comes via OPENAI_API_KEY in the environment (OPENAI_BASE_URL is no longer read)."""
    import yaml
    _mk(ctx, "hermes")
    hh = ctx.home / "hermes"
    # key_env names the env var that holds the key; without it the custom provider sends "no-key-required".
    (hh / "config.yaml").write_text(yaml.safe_dump({"model": {"default": ctx.model, "provider": "custom", "base_url": ctx.base_url,
                                                              "key_env": ctx.api_key_env}}))
    env = base_env(ctx, {"HERMES_HOME": str(hh), "TERMINAL_CWD": str(ctx.workdir),
                         "OPENAI_API_KEY": os.environ.get(ctx.api_key_env, "")})
    cmd = rt.build_hermes_cmd(_ns(provider="custom", model=ctx.model), ctx.prompt, hh, ctx.workdir)
    return Invocation(cmd, env, lambda out, err: rt.scrape_hermes_metrics(hh))


def build_cline(ctx: Ctx) -> Invocation:
    """cline 3.0.74. Passing -P/-m on a run REPLACES the saved provider settings (dropping apiKey/baseUrl),
    and the run command has no base-URL flag. The supported flow is therefore `cline auth` (which writes
    <data-dir>/settings/providers.json with key, model and base URL) followed by a plain run that uses the
    saved provider. The key is read from the environment by the shell, so it never appears in an argv; the
    settings file Cline writes is excluded from exports (SECRET_FILE_GLOBS: providers.json) and purged."""
    _mk(ctx, "cline/data/settings", "cline/config")
    data, conf = ctx.home / "cline/data", ctx.home / "cline/config"
    env = base_env(ctx)
    run = rt.build_cline_cmd(_ns(provider="", model=""), ctx.prompt, ctx.home / "cline", ctx.workdir)
    run[1:1] = ["--yolo"]    # one-shot tool approvals
    script = ('cline auth -p openai-compatible -k "$%s" -b %s -m %s --data-dir %s --config %s >/dev/null 2>&1 '
              '&& exec "$@"') % (ctx.api_key_env, shlex.quote(ctx.base_url), shlex.quote(ctx.model),
                                 shlex.quote(str(data)), shlex.quote(str(conf)))
    cmd = ["sh", "-c", script, "cline-bench", *run]
    return Invocation(cmd, env, lambda out, err: rt.metrics_from_cline_events(out),
                      ["`cline auth` writes the provider settings; the key comes from the environment, not argv",
                       "API key lives in a file under the private trial home; purged + never exported"])


def build_kilo(ctx: Ctx) -> Invocation:
    """@kilocode/cli 7.8.18 reads HOME/.config/kilo/kilo.json (OpenCode-fork provider schema)."""
    _mk(ctx, ".config/kilo")
    (ctx.home / ".config/kilo/kilo.json").write_text(json.dumps(kilo_json(ctx), indent=2))
    env = base_env(ctx)
    cmd = rt.build_kilo_cmd(_ns(provider=ctx.provider_id, model=ctx.model), ctx.prompt, ctx.workdir)
    return Invocation(cmd, env, lambda out, err: rt.metrics_from_kilo_events(out))


def omp_models_yml(ctx: Ctx) -> dict:
    return {"providers": {ctx.provider_id: {
        "baseUrl": ctx.base_url, "api": "openai-completions", "apiKey": ctx.api_key_env,
        "models": [{"id": ctx.model, "name": ctx.model, "contextWindow": 128000, "maxTokens": 16384}]}}}


def build_omp(ctx: Ctx) -> Invocation:
    """oh-my-pi 18.16.0 (can1357/oh-my-pi) reads HOME/.omp/agent/models.yml, not Pi's dirs / models.json."""
    import yaml
    _mk(ctx, ".omp/agent", "omp-sessions")
    (ctx.home / ".omp/agent/models.yml").write_text(yaml.safe_dump(omp_models_yml(ctx), sort_keys=False))
    env = base_env(ctx)
    cmd = rt.build_omp_cmd(_ns(provider=ctx.provider_id, model=ctx.model), ctx.prompt, ctx.home / "omp-sessions", ctx.workdir)
    sd = ctx.home / "omp-sessions"
    return Invocation(cmd, env, lambda out, err: rt.scrape_pi_metrics(sd))


def build_dsh(ctx: Ctx) -> Invocation:
    _mk(ctx, "dsh")
    env = base_env(ctx, {"DSH_HOME": str(ctx.home / "dsh")})
    py = os.environ.get("DSH_PYTHON") or "python3"
    cmd = [py, str(SCRIPTS_DIR / "dsh_sdk_run.py"), "--provider", "custom", "--model", ctx.model,
           "--base-url", ctx.base_url, "--api-key-env", ctx.api_key_env, "--cwd", str(ctx.workdir), "--prompt", ctx.prompt]
    dh = ctx.home / "dsh"
    return Invocation(cmd, env, lambda out, err: rt.scrape_dsh_metrics(dh))


BUILDERS: dict[str, Callable[[Ctx], Invocation]] = {
    "loop": build_loop, "pi": build_pi, "goose": build_goose, "opencode": build_opencode, "hermes": build_hermes,
    "cline": build_cline, "kilo": build_kilo, "omp": build_omp, "deepseek-harness": build_dsh,
}
assert set(BUILDERS) == set(HARNESSES)


def build(harness: str, ctx: Ctx) -> Invocation:
    if harness not in BUILDERS:
        raise KeyError(f"unknown harness {harness}")
    return BUILDERS[harness](ctx)


def redacted_cmd(cmd: list[str], secrets: list[str]) -> str:
    s = shlex.join(cmd)
    for sec in secrets:
        if sec:
            s = s.replace(sec, "[REDACTED]")
    return s
