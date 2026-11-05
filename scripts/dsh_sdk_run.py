#!/usr/bin/env python3
"""Run one task through DeepSeek Harness via its Python SDK, for run_task.py.

Why this exists instead of `dsh --profile headless "<task>"`: the headless
profile takes no provider/model flags - they come from plugin config
(`agent-default-model` defaults to deepseek-official / deepseek-flash) - so
pointing it at OpenRouter needs a plugin patch, and the shipped profile has no
file-editing tool. The SDK path below is the one eval-bench's adapter already
runs end to end (sdk-minimal profile + a provider-routing patch + the
str-replace editor tool), so this reuses that proven setup rather than
guessing at patch syntax for the CLI.

Must run under a Python that has `deepseek-harness-sdk` installed (eval-bench's
.venv does; run_task.py finds it via --dsh-python / DSH_PYTHON). Prints the
final assistant message on stdout, like the other harnesses' print modes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROUTES = {
    # provider -> (dsh llm route, default api key env, default baseURL or None)
    "deepseek-official": ("deepseek-official", "DEEPSEEK_API_KEY", None),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "openai": ("openai", "OPENAI_API_KEY", None),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", None),
    # any OpenAI-compatible endpoint; needs --base-url (and optionally --api-key-env)
    "custom": ("custom", "BENCH_API_KEY", None),
}

EDITOR_PATCH = """\
- insert:
    - id: fs-local
      name: '@deepseek-ai/dsh-fs-local'
      config:
        cwd: !!js process.cwd()
    - id: tool-str-replace-editor
      name: '@deepseek-ai/dsh-tool-str-replace-editor'
"""


def provider_patch(route: str, api_key_env: str, base_url: str | None, model: str, hand_declared: bool) -> str:
    cfg: dict = {"apiKeyEnv": api_key_env}
    if hand_declared:
        cfg["api"] = "openai-completions"
        cfg["baseURL"] = base_url
        cfg["models"] = [{"id": model}]
    # JSON is valid YAML, which avoids hand-rolling nested indentation.
    return (
        "- insert:\n    - id: llm-pi-ai-bench\n      name: '@deepseek-ai/dsh-llm-pi-ai'\n      config: "
        + json.dumps({"providers": {route: cfg}})
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--provider", default="deepseek-official")
    ap.add_argument("--model", required=True)
    ap.add_argument("--cwd", default=os.getcwd(), help="working directory for the agent (default: this process's cwd)")
    ap.add_argument("--dsh-home", default=os.environ.get("DSH_HOME", ""), help="default: $DSH_HOME")
    ap.add_argument("--profile", default="sdk-minimal")
    ap.add_argument("--base-url", default="", help="provider=custom only: OpenAI-compatible base URL")
    ap.add_argument("--api-key-env", default="", help="provider=custom only: env var NAME holding the key")
    ap.add_argument("--max-tokens", type=int, default=49152)
    ap.add_argument("--no-editor", action="store_true", help="shell-only, like eval-bench's default")
    args = ap.parse_args()

    if not args.dsh_home:
        print("dsh_sdk_run: pass --dsh-home or set DSH_HOME", file=sys.stderr)
        return 2
    if args.provider not in ROUTES:
        print(f"dsh_sdk_run: unsupported provider {args.provider!r} (known: {', '.join(ROUTES)})", file=sys.stderr)
        return 2
    route, key_env, base_url = ROUTES[args.provider]
    if args.provider == "custom":
        if not args.base_url:
            print("dsh_sdk_run: --base-url is required for provider=custom", file=sys.stderr)
            return 2
        base_url = args.base_url
        key_env = args.api_key_env or key_env
    if not os.environ.get(key_env):
        print(f"dsh_sdk_run: {key_env} is not set", file=sys.stderr)
        return 2

    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError:
        print("dsh_sdk_run: 'deepseek-harness-sdk' is not installed in this Python (see --dsh-python)", file=sys.stderr)
        return 2

    patches: list[Path] = []

    def write_patch(content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".patch.yml", delete=False)
        f.write(content)
        f.close()
        patches.append(Path(f.name))
        return f.name

    patch_args: list[str] = []
    if not args.no_editor and args.profile == "sdk-minimal":
        patch_args.append(write_patch(EDITOR_PATCH))
    if args.provider != "deepseek-official":
        patch_args.append(write_patch(provider_patch(route, key_env, base_url, args.model, hand_declared=args.provider in ("openrouter", "custom"))))

    Path(args.dsh_home).mkdir(parents=True, exist_ok=True)
    os.environ["PWD"] = args.cwd
    try:
        with DeepSeekHarness(
            provider=route,
            model=args.model,
            max_tokens=args.max_tokens,
            cwd=args.cwd,
            dsh_home=args.dsh_home,
            profile=args.profile,
            patches=tuple(patch_args),
        ) as harness:
            result = harness.run(args.prompt, session_id="bench")
        print(result.final_response)
        return 0
    finally:
        for p in patches:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
