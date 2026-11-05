"""Repository-root resolution. In the runner image BENCH_ROOT points at the
baked-in copy of this repository (/opt/bench)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("BENCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
SCRIPTS_DIR = REPO_ROOT / "scripts"
TASKS_DIR = REPO_ROOT / "tasks"
FIXTURES_DIR = REPO_ROOT / "fixtures"
VERIFIERS_DIR = REPO_ROOT / "verifiers"
CONTROLS_DIR = REPO_ROOT / "controls"


def ensure_scripts_importable() -> None:
    """The original scripts/ are flat modules (``from validate_tasks import ...``)."""
    p = str(SCRIPTS_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
