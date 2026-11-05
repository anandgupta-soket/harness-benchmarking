import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_image_installs_every_pinned_harness_and_runner_dependency():
    cfg = yaml.safe_load((ROOT / "configs/glm53.yaml").read_text())
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    manifest = json.loads((ROOT / "docker/image-manifest.json").read_text())
    npm = json.loads((ROOT / "docker/package.json").read_text())
    lock = json.loads((ROOT / "docker/package-lock.json").read_text())

    assert cfg["harnesses"]["loop"]["version"] == "0.6.0"
    for harness, item in cfg["harnesses"].items():
        assert manifest["harnesses"][harness]["version"] == item["version"]
    assert cfg["harnesses"]["pi"]["version"] == npm["dependencies"]["@mariozechner/pi-coding-agent"]
    assert cfg["harnesses"]["opencode"]["version"] == npm["dependencies"]["opencode-ai"]
    assert cfg["harnesses"]["cline"]["version"] == npm["dependencies"]["cline"]
    assert cfg["harnesses"]["kilo"]["version"] == npm["dependencies"]["@kilocode/cli"]
    assert cfg["harnesses"]["goose"]["version"] in dockerfile
    assert cfg["harnesses"]["hermes"]["version"] in dockerfile
    assert cfg["harnesses"]["omp"]["version"] in dockerfile
    assert cfg["harnesses"]["deepseek-harness"]["version"] in dockerfile
    for package, version in npm["dependencies"].items():
        assert lock["packages"][f"node_modules/{package}"]["version"] == version
    assert "uv sync --frozen" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "npm ci --prefix /opt/bench/fixtures/dark-mode-site" in dockerfile
    assert "kubectl.sha256" in dockerfile and "KUBECTL_VERSION=1.30.14" in dockerfile
    assert "UV_PYTHON_INSTALL_DIR=/opt/python" in dockerfile
    assert "chmod 0711 /opt/bench/fixtures /opt/bench/fixtures/dark-mode-site" in dockerfile
    assert "RUSTUP_HOME=/usr/local/rustup" in dockerfile and "/usr/local/cargo/bin" in dockerfile


def test_image_keeps_trusted_checks_and_fixture_goldens_root_only():
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    assert "chmod -R go-rwx /opt/bench/controls /opt/bench/verifiers /opt/bench/tests /opt/bench/fixtures" in dockerfile
    assert "chmod 0400 /opt/bench/bench/verification.py" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "find /opt/bench -type d -name __pycache__" in dockerfile


def test_docker_context_excludes_local_credentials_and_generated_data():
    ignore = (ROOT / ".dockerignore").read_text()
    for pattern in (".git", "**/.env", "**/auth.json", "**/credentials*", "**/secrets*", "**/*.pem", "**/*.key",
                    "**/node_modules", "**/target", "runs/out", "results", "exports"):
        assert pattern in ignore


def test_ghcr_workflow_is_manual_and_uses_github_token_only():
    workflow = yaml.safe_load((ROOT / ".github/workflows/build-image.yml").read_text())
    triggers = workflow.get("on", workflow.get(True, {}))  # PyYAML's YAML 1.1 parser treats `on` as boolean.
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"]["packages"] == "write"
    content = (ROOT / ".github/workflows/build-image.yml").read_text()
    assert "secrets.GITHUB_TOKEN" in content
    assert "platforms: linux/amd64" in content
    assert "runs-on: ubuntu-latest" in content
