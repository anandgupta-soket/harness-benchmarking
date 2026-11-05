#!/usr/bin/env python3
"""Validate tasks/*.yaml fixtures (stdlib only, no PyYAML)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = ROOT / "tasks"

REQUIRED_TOP = {"id", "suite", "category", "title", "repo", "prompt", "success", "limits", "operator_policy"}
VALID_SUITES = {"core-coding", "agentic", "context"}
VALID_CATEGORIES = {"bugfix", "feature", "refactor", "tooling", "understanding"}
VALID_REPO_TYPES = {"fixture", "loop", "external"}
VALID_SUCCESS_TYPES = {"command", "file_contains", "manual"}


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a restricted YAML subset used by task fixtures."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, root)]
    lines = text.splitlines()
    i = 0
    key_re = re.compile(r"^(\s*)([A-Za-z0-9_]+):\s*(.*)$")

    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        m = key_re.match(line)
        if not m:
            continue

        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and indent < stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if rest == "|":
            block: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "":
                    block.append("")
                    i += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent <= indent and nxt.strip():
                    break
                block.append(nxt[nxt_indent:] if nxt_indent > indent else nxt.lstrip())
                i += 1
            value: Any = "\n".join(block).rstrip("\n")
        elif rest == "":
            value = {}
            stack.append((indent + 2, value))
        elif rest.startswith("- "):
            # Start of inline list under key — collect subsequent `- item` lines
            items = [rest[2:].strip()]
            while i < len(lines):
                nxt = lines[i]
                if re.match(r"^\s+-\s+", nxt):
                    items.append(nxt.strip()[2:].strip())
                    i += 1
                else:
                    break
            value = items
        else:
            value = rest.strip()
            # Unwrap one matching quote pair only; the old chained strip()
            # also ate lone leading/trailing quotes, which truncated values
            # like `sh -c "a && b"` into an unbalanced command.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value.isdigit():
                value = int(value)

        if isinstance(value, dict) and key in parent and isinstance(parent[key], dict):
            stack.append((indent + 2, parent[key]))
            continue

        parent[key] = value
        if isinstance(value, dict):
            stack.append((indent + 2, value))

    return root


def load_suite_index() -> list[Path]:
    # SUITE_INDEX=suite-long.yaml selects another index (default: suite.yaml)
    suite_file = TASKS_DIR / (os.environ.get("SUITE_INDEX", "").strip() or "suite.yaml")
    if not suite_file.is_file():
        return sorted(p for p in TASKS_DIR.rglob("*.yaml") if p.name not in {"suite.yaml", "_template.yaml"})

    paths: list[Path] = []
    for line in suite_file.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s+-\s+(\S+\.yaml)\s*$", line)
        if m:
            paths.append(TASKS_DIR / m.group(1))
    return paths


def check_path_exists(repo: dict[str, Any]) -> str | None:
    repo_type = repo.get("type")
    rel = Path(str(repo.get("path", "")))
    if repo_type == "external":
        return None
    target = ROOT / rel
    if not target.exists():
        return f"repo.path does not exist: {rel}"
    return None


def validate_task(path: Path) -> list[str]:
    errors: list[str] = []
    if path.name.startswith("_"):
        return errors

    try:
        data = parse_simple_yaml(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"{path}: parse error: {e}"]

    missing = REQUIRED_TOP - set(data.keys())
    if missing:
        errors.append(f"{path}: missing fields: {', '.join(sorted(missing))}")

    if data.get("suite") not in VALID_SUITES:
        errors.append(f"{path}: invalid suite {data.get('suite')!r}")

    if data.get("category") not in VALID_CATEGORIES:
        errors.append(f"{path}: invalid category {data.get('category')!r}")

    repo = data.get("repo")
    if not isinstance(repo, dict):
        errors.append(f"{path}: repo must be a mapping")
    else:
        for key in ("type", "path", "commit"):
            if key not in repo:
                errors.append(f"{path}: repo missing {key}")
        if repo.get("type") not in VALID_REPO_TYPES:
            errors.append(f"{path}: invalid repo.type {repo.get('type')!r}")
        else:
            err = check_path_exists(repo)
            if err:
                errors.append(f"{path}: {err}")

    limits = data.get("limits")
    if not isinstance(limits, dict):
        errors.append(f"{path}: limits must be a mapping")
    else:
        for key in ("timeout_minutes", "max_turns", "max_tokens"):
            if key not in limits:
                errors.append(f"{path}: limits missing {key}")

    success = data.get("success")
    if not isinstance(success, dict):
        errors.append(f"{path}: success must be a mapping")
    else:
        stype = success.get("type")
        if stype not in VALID_SUCCESS_TYPES:
            errors.append(f"{path}: invalid success.type {stype!r}")
        elif stype == "command":
            for key in ("command", "expect_exit"):
                if key not in success:
                    errors.append(f"{path}: success.command missing {key}")
        elif stype == "file_contains":
            for key in ("path", "pattern"):
                if key not in success:
                    errors.append(f"{path}: success.file_contains missing {key}")
        elif stype == "manual" and "checklist" not in success:
            errors.append(f"{path}: success.manual missing checklist")

    if not str(data.get("prompt", "")).strip():
        errors.append(f"{path}: prompt is empty")

    return errors


def main() -> None:
    task_paths = load_suite_index()
    all_errors: list[str] = []
    checked = 0

    for path in task_paths:
        if not path.is_file():
            all_errors.append(f"missing task file listed in suite.yaml: {path}")
            continue
        checked += 1
        all_errors.extend(validate_task(path))

    if all_errors:
        print(f"FAIL ({len(all_errors)} issues, {checked} tasks checked):\n", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    print(f"OK: {checked} task(s) validated")


if __name__ == "__main__":
    main()
