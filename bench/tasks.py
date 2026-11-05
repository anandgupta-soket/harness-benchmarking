"""Task loading. Reuses the original tasks/*.yaml + scripts/validate_tasks parser."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import REPO_ROOT, TASKS_DIR, ensure_scripts_importable

# The 13 benchmark tasks (12 core-coding + AGENT-01), in canonical order.
TASK_IDS = (
    "BUG-01", "FEAT-01", "FEAT-02", "FEAT-03", "FEAT-04", "FEAT-05", "FEAT-06",
    "REF-01", "TOOL-01", "WEB-BUG-01", "WEB-FEAT-01", "WEB-REF-01", "AGENT-01",
)


@dataclass(frozen=True)
class Task:
    id: str
    path: str            # relative to repo root
    fixture: str         # relative fixture dir, e.g. fixtures/mini-calc
    prompt: str
    timeout_minutes: int
    max_turns: int
    max_tokens: int
    category: str
    title: str


def load_tasks(ids: tuple[str, ...] | list[str] | None = None) -> list[Task]:
    ensure_scripts_importable()
    from validate_tasks import load_suite_index, parse_simple_yaml  # type: ignore

    found: dict[str, Task] = {}
    for path in load_suite_index():
        data: dict[str, Any] = parse_simple_yaml(path.read_text(encoding="utf-8"))
        tid = str(data["id"])
        repo = data["repo"]
        lim = data["limits"]
        if repo.get("type") != "fixture":
            raise ValueError(f"{tid}: only fixture repos are supported")
        found[tid] = Task(
            id=tid, path=str(path.relative_to(REPO_ROOT)), fixture=str(repo["path"]),
            prompt=str(data["prompt"]).strip(), timeout_minutes=int(lim["timeout_minutes"]),
            max_turns=int(lim["max_turns"]), max_tokens=int(lim["max_tokens"]),
            category=str(data["category"]), title=str(data["title"]),
        )
    want = list(ids) if ids else list(TASK_IDS)
    missing = [t for t in want if t not in found]
    if missing:
        raise ValueError(f"tasks missing from suite: {missing}")
    return [found[t] for t in want]


def get_task(task_id: str) -> Task:
    return load_tasks([task_id])[0]


def tier(task: Task) -> str:
    """Correctness tiers for the report: grouped by category."""
    return task.category
