import json
from pathlib import Path

import pytest

from bench.config import HARNESSES
from bench import harnesses as H


def ctx(tmp_path, **kw):
    home, wd = tmp_path / "home", tmp_path / "wd"
    home.mkdir(), wd.mkdir()
    d = dict(workdir=wd, home=home, prompt="do the thing; $(rm -rf /)", base_url="https://api.soket.ai/v1",
             model="GLM-5.3", provider_id="custom-test-soket", api_key_env="BENCH_API_KEY")
    d.update(kw)
    return H.Ctx(**d)


def test_loop_models_json_is_array_with_apikeyenv_list(tmp_path):
    c = ctx(tmp_path)
    inv = H.build("loop", c)
    models = json.loads((c.home / "agent" / "models.json").read_text())
    assert models == {"providers": [{"id": "custom-test-soket", "name": "custom-test-soket",
                                     "baseUrl": "https://api.soket.ai/v1", "apiKeyEnv": ["BENCH_API_KEY"],
                                     "models": [{"id": "GLM-5.3", "name": "GLM-5.3"}]}]}
    st = json.loads((c.home / "agent" / "settings.json").read_text())
    assert st["defaultProjectTrust"] == "always" and st["toolPermissions"]["bash"] == "allow"
    assert st["defaultProvider"] == "custom-test-soket" and st["defaultModel"] == "GLM-5.3"
    assert inv.cmd[1:3] == ["--permission-mode", "full-access"]
    assert inv.cmd[-2] == "--print" and inv.cmd[-1] == c.prompt     # prompt is one argv element
    assert H.loop_models_json(c) != H.pi_models_json(c)              # not the Pi dict schema


@pytest.mark.parametrize("h", HARNESSES)
def test_every_adapter_builds_without_secret_in_files_or_argv(tmp_path, h, monkeypatch):
    monkeypatch.setenv("BENCH_API_KEY", "sk-SECRETSECRETSECRET1234")
    c = ctx(tmp_path)
    inv = H.build(h, c)
    assert inv.cmd and all(isinstance(x, str) for x in inv.cmd)
    assert "sk-SECRETSECRETSECRET1234" not in " ".join(inv.cmd)
    import fnmatch
    from bench.safecopy import SECRET_FILE_GLOBS
    for f in c.home.rglob("*"):
        if f.is_file() and "sk-SECRETSECRETSECRET1234" in f.read_text(errors="ignore"):
            # the only allowed holder is cline's 0600 provider settings, which is never exported
            assert h == "cline" and f.name == "providers.json" and (f.stat().st_mode & 0o777) == 0o600, f
            assert any(fnmatch.fnmatch(f.name, g) for g in SECRET_FILE_GLOBS)
    assert "HOME" in inv.env and inv.env["HOME"] == str(c.home)
    assert "BENCH_ATTEMPT_ID" not in inv.env       # worker env is not inherited


def test_opencode_argv_matches_1_19_5(tmp_path):
    c = ctx(tmp_path)
    inv = H.build("opencode", c)
    assert inv.cmd == ["opencode", "run", "--format", "json", "--dir", str(c.workdir),
                       "--model", "custom-test-soket/GLM-5.3", c.prompt]
    assert "--standalone" not in inv.cmd and "--auto" not in inv.cmd
    conf = json.loads((c.home / "opencode.json").read_text())
    assert conf["permission"]["bash"] == "allow" and conf["provider"]["custom-test-soket"]["options"]["baseURL"] == c.base_url
    assert "BENCH_API_KEY" in conf["provider"]["custom-test-soket"]["options"]["apiKey"]


def test_audited_schemas(tmp_path):
    import yaml
    c = ctx(tmp_path)
    H.build("goose", c)
    g = json.loads((c.home / "goose-root/config/custom_providers/custom-test-soket.json").read_text())
    assert g["base_url"] == "https://api.soket.ai/v1/chat/completions" and g["api_key_env"] == "BENCH_API_KEY"
    sub = tmp_path / "h"
    sub.mkdir()
    c2 = ctx(sub)
    H.build("omp", c2)
    y = yaml.safe_load((c2.home / ".omp/agent/models.yml").read_text())
    assert y["providers"]["custom-test-soket"]["api"] == "openai-completions" and y["providers"]["custom-test-soket"]["apiKey"] == "BENCH_API_KEY"
    H.build("hermes", c2)
    hy = yaml.safe_load((c2.home / "hermes/config.yaml").read_text())
    assert hy == {"model": {"default": "GLM-5.3", "provider": "custom", "base_url": "https://api.soket.ai/v1",
                            "key_env": "BENCH_API_KEY"}}
    H.build("kilo", c2)
    kilo = json.loads((c2.home / ".config/kilo/kilo.json").read_text())
    kp = kilo["provider"]["custom-test-soket"]
    assert kp["npm"] == "@ai-sdk/openai-compatible" and kp["env"] == ["BENCH_API_KEY"]
    assert kp["options"]["baseURL"] == "https://api.soket.ai/v1"
    assert kp["models"]["GLM-5.3"]["limit"] == {"context": 128000, "output": 16384}


def test_cline_authenticates_then_runs_without_provider_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_API_KEY", "sk-abc123")
    c = ctx(tmp_path)
    inv = H.build("cline", c)
    # `cline auth` runs first and writes the settings; the run must not pass -P/-m/-k (they replace saved settings).
    assert inv.cmd[:2] == ["sh", "-c"] and "cline auth -p openai-compatible" in inv.cmd[2]
    assert '-k "$BENCH_API_KEY"' in inv.cmd[2] and "&& exec" in inv.cmd[2]
    run = inv.cmd[4:]
    assert run[0] == "cline" and "--yolo" in run and "--json" in run
    assert "-P" not in run and "-m" not in run and "-k" not in run
    assert "sk-abc123" not in " ".join(inv.cmd) and inv.env["BENCH_API_KEY"] == "sk-abc123"


def test_cline_3074_tool_calls_are_counted_from_agent_events():
    import json as _j, sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_task as rt
    ev = lambda **kw: _j.dumps({"type": "agent_event", "event": kw})
    out = "\n".join([
        ev(type="content_start", contentType="reasoning", reasoning="x"),
        ev(type="content_start", contentType="tool", toolName="run_commands", input={}),
        ev(type="content_end", contentType="tool", toolName="run_commands", output=[]),
        ev(type="content_start", contentType="tool", toolName="editor", input={}),
        ev(type="content_start", contentType="text", text="done"),
        _j.dumps({"type": "run_result", "iterations": 2, "aggregateUsage": {"inputTokens": 100, "outputTokens": 10}, "model": {"id": "GLM-5.3"}}),
    ])
    m = rt.metrics_from_cline_events(out)
    assert m["tool_calls"] == 2 and m["total_tokens"] == 110 and m["assistant_turns"] == 2
