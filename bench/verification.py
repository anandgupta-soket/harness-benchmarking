"""Trusted, offline correctness checks for the coding tasks.

Candidate files are overlaid onto fresh copies of the checked-in fixtures. Tests,
manifests, scripts and browser tooling always come from this verifier's checkout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .paths import FIXTURES_DIR, VERIFIERS_DIR

_ALLOW: dict[str, tuple[str, ...]] = {
    "BUG-01": ("src/lib.rs",), "AGENT-01": ("src/lib.rs",),
    "FEAT-01": ("src/lib.rs", "src/main.rs"),
    "REF-01": ("src/main.rs",), "TOOL-01": ("src/lib.rs", "src/main.rs"),
    "WEB-BUG-01": ("js/todo.js",), "WEB-FEAT-01": ("js/todo.js",),
    "WEB-REF-01": ("js/app.js",),
    "FEAT-02": ("index.html", "about.html", "services.html", "contact.html", "js/app.js", "css/style.css"),
    "FEAT-03": ("services.html", "js/app.js", "css/style.css"),
    "FEAT-04": ("services.html", "index.html", "about.html", "contact.html", "js/app.js", "css/style.css"),
    "FEAT-05": ("contact.html", "js/app.js", "css/style.css"),
    "FEAT-06": ("index.html", "about.html", "services.html", "contact.html", "js/app.js", "css/style.css"),
}
_FIXTURE = {
    "BUG-01": "mini-calc-buggy", "AGENT-01": "mini-calc-buggy", "TOOL-01": "mini-calc-buggy",
    "FEAT-01": "mini-calc", "REF-01": "mini-calc",
    "WEB-BUG-01": "mini-web", "WEB-FEAT-01": "mini-web", "WEB-REF-01": "mini-web",
    **{k: "dark-mode-site" for k in ("FEAT-02", "FEAT-03", "FEAT-04", "FEAT-05", "FEAT-06")},
}


def _safe_overlay(task_id: str, submission: Path, destination: Path) -> None:
    """Overlay only explicitly permitted regular files; reject symlinks and extras."""
    if task_id not in _ALLOW:
        raise ValueError(f"unknown task: {task_id}")
    src = submission.resolve(strict=True)
    if not src.is_dir():
        raise ValueError("submission must be a directory")
    fixture = FIXTURES_DIR / _FIXTURE[task_id]
    baseline_sources = {p.relative_to(fixture).as_posix() for p in fixture.rglob("*")
                        if p.is_file() and _is_production_source(task_id, p.relative_to(fixture).as_posix())}
    submitted_files: set[str] = set()
    for p in src.rglob("*"):
        rel = p.relative_to(src).as_posix()
        if p.is_symlink():
            raise ValueError(f"submission contains a symlink: {rel}")
        if p.is_dir():
            continue
        if not p.is_file():
            raise ValueError(f"submission contains a non-regular file: {rel}")
        if _is_production_source(task_id, rel):
            submitted_files.add(rel)
        else:
            # Full workspace snapshots are normal. Tests, manifests, scripts,
            # and generated files are never copied into the trusted fixture.
            continue
        target = (destination / rel).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise ValueError(f"submission path escapes fixture: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
    missing = sorted(baseline_sources - submitted_files)
    if missing:
        raise ValueError(f"submission deleted trusted production files: {', '.join(missing)}")


def _is_production_source(task_id: str, rel: str) -> bool:
    path = Path(rel)
    if path.suffix not in {".rs", ".js", ".css", ".html"}:
        return False
    if task_id.startswith("WEB-"):
        return path.parts[0] == "js" and path.suffix == ".js"
    if task_id.startswith("FEAT-") and task_id != "FEAT-01":
        return (len(path.parts) == 1 and path.suffix == ".html") or path.parts[0] in {"js", "css"}
    return path.parts[0] == "src" and path.suffix == ".rs"


def _run(command: list[str], cwd: Path, timeout: int = 180) -> tuple[bool, str]:
    env = os.environ.copy()
    env.update({"CARGO_NET_OFFLINE": "true", "npm_config_offline": "true", "CI": "1"})
    # Dependencies are baked from the trusted fixture lockfile in the worker
    # image. Resolve them outside candidate workspaces so submissions cannot
    # shadow grader packages or trigger a network install.
    trusted_node_modules = FIXTURES_DIR / "dark-mode-site" / "node_modules"
    if trusted_node_modules.is_dir():
        env["NODE_PATH"] = str(trusted_node_modules)
    try:
        p = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return p.returncode == 0, p.stdout[-12000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _browser_nav_check(site: Path) -> tuple[bool, str]:
    """Exercise keyboard focus and trap behavior in a real Chromium page."""
    try:
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from playwright.sync_api import sync_playwright

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, directory=str(site), **kwargs)
            def log_message(self, *_: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                    button = page.locator("#nav-toggle")
                    links = page.locator("#site-nav a")
                    last = links.nth(links.count() - 1)
                    button.click()
                    if not page.evaluate("document.activeElement === document.querySelector('#site-nav a')"):
                        return False, "opening menu did not focus first link"
                    page.keyboard.press("Escape")
                    if page.locator("#site-nav").evaluate("el => el.classList.contains('open')") or page.evaluate("document.activeElement.id") != "nav-toggle":
                        return False, "Escape did not close and restore toggle focus"
                    button.click()
                    last.focus()
                    page.keyboard.press("Tab")
                    if page.evaluate("document.activeElement.id") != "nav-toggle":
                        return False, "Tab from final link escaped the menu"
                    button.focus()
                    page.keyboard.press("Shift+Tab")
                    if not last.evaluate("el => el === document.activeElement"):
                        return False, "Shift+Tab from toggle did not wrap to final link"
                    return True, "Chromium focus, Escape, Tab, and Shift+Tab checks passed"
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    except Exception as e:
        return False, f"Playwright/Chromium unavailable or check failed: {type(e).__name__}: {e}"


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(ok), "detail": detail}


def _without_comments_and_strings(source: str) -> str:
    """Blank Rust/JS comments and quoted literals before structural checks."""
    # Sufficient for fixture syntax: preserve newlines and offsets, remove
    # line/block comments and string/template literals (including escapes).
    pattern = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
    return pattern.sub(lambda m: "\n" * m.group(0).count("\n"), source)


def _body_after(source: str, signature: re.Match[str] | None) -> tuple[str, int, int]:
    if signature is None:
        return "", -1, -1
    opening = source.find("{", signature.end())
    if opening < 0:
        return "", -1, -1
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{": depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index], opening, index + 1
    return "", -1, -1


def verify(task_id: str, submission: Path, output_dir: Path) -> dict[str, Any]:
    """Run trusted task checks. Result schema is stable for verifier Jobs and local use."""
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        fixture = FIXTURES_DIR / _FIXTURE[task_id]
        with tempfile.TemporaryDirectory(prefix=f"verify-{task_id.lower()}-") as td:
            work = Path(td) / "fixture"
            shutil.copytree(fixture, work, ignore=shutil.ignore_patterns("node_modules", "target", ".git"))
            _safe_overlay(task_id, Path(submission), work)
            if task_id in {"BUG-01", "FEAT-01", "AGENT-01", "REF-01", "TOOL-01"}:
                # Trusted integration tests are injected after overlay so a
                # candidate cannot edit or delete the grader's assertions.
                tests_dir = work / "tests"
                tests_dir.mkdir(exist_ok=True)
                if task_id in {"BUG-01", "AGENT-01", "TOOL-01"}:
                    contract = '''use mini_calc_buggy::{add, is_even, mul};
#[test] fn trusted_arithmetic_contract() {
    assert_eq!(add(17, 25), 42); assert_eq!(add(-8, 3), -5);
    assert_eq!(mul(-7, 6), -42); assert!(is_even(-8)); assert!(!is_even(7));
}'''
                elif task_id == "FEAT-01":
                    contract = '''use mini_calc::{add, mul, parse_verbose_flag};
#[test] fn trusted_arithmetic_contract() { assert_eq!(add(17,25),42); assert_eq!(mul(-7,6),-42); }'''
                    contract += '''
#[test] fn trusted_verbose_contract() {
    assert!(parse_verbose_flag(&vec!["--verbose".into()]));
    assert!(parse_verbose_flag(&vec!["-v".into()]));
    assert!(!parse_verbose_flag(&vec!["-x".into()]));
}'''
                else:
                    contract = '''use mini_calc::{add, mul};
#[test] fn trusted_arithmetic_contract() { assert_eq!(add(17,25),42); assert_eq!(mul(-7,6),-42); }'''
                (tests_dir / "trusted_verifier.rs").write_text(contract)
                ok, out = _run(["cargo", "test", "--offline"], work)
                checks.append(_check("trusted cargo tests", ok, out))
                for op, a, b, answer in (("add", "2", "3", "5"), ("mul", "-4", "6", "-24")):
                    cli_ok, cli_out = _run(["cargo", "run", "--offline", "-q", "--", op, a, b], work)
                    checks.append(_check(f"CLI {op} behavior", cli_ok and cli_out.strip() == answer, cli_out))
                bad_ok, bad_out = _run(["cargo", "run", "--offline", "-q", "--", "div", "2", "3"], work)
                checks.append(_check("CLI rejects unknown operation", not bad_ok and "unknown op" in bad_out, bad_out))
                for bad_args, message in ((["add", "nope", "3"], "a must be integer"),
                                          (["add", "2", "nope"], "b must be integer")):
                    bad_ok, bad_out = _run(["cargo", "run", "--offline", "-q", "--", *bad_args], work)
                    checks.append(_check(f"CLI preserves {message} error", not bad_ok and message in bad_out, bad_out))
                if task_id == "FEAT-01":
                    ok, out = _run(["cargo", "run", "--offline", "-q", "--", "add", "2", "3", "--verbose"], work)
                    ok = ok and re.search(r"(?m)^5\s*$", out) is not None and "verbose: add 2 3 -> 5" in out
                    checks.append(_check("long verbose flag behavior", ok, out))
                    ok, out = _run(["cargo", "run", "--offline", "-q", "--", "add", "2", "3", "-v"], work)
                    ok = ok and re.search(r"(?m)^5\s*$", out) is not None and "verbose: add 2 3 -> 5" in out
                    checks.append(_check("short verbose flag preserved", ok, out))
                if task_id == "REF-01":
                    text = _without_comments_and_strings((work / "src/main.rs").read_text())
                    declared = re.search(r"(?m)^\s*fn\s+parse_i32_arg\s*\(", text)
                    body, start, end = _body_after(text, declared)
                    outside = text[:declared.start()] + text[end:] if declared and end >= 0 else ""
                    calls = len(re.findall(r"\bparse_i32_arg\s*\(", outside))
                    direct_parses = len(re.findall(r"\.parse\s*(?:::\s*<\s*i32\s*>|\(\s*\))", outside))
                    helper_parses = len(re.findall(r"\.parse\s*\(", body)) + len(re.findall(r"i32\s*::\s*from_str\s*\(", body))
                    ok = bool(declared) and calls >= 2 and direct_parses == 0 and helper_parses == 1
                    checks.append(_check("parse helper used for both operands", ok, f"calls={calls}, outside parses={direct_parses}, helper parses={helper_parses}"))
                    ok2, out2 = _run(["cargo", "run", "--offline", "-q", "--", "add", "2", "3"], work)
                    checks.append(_check("CLI behavior preserved", ok2 and out2.strip() == "5", out2))
                if task_id == "TOOL-01":
                    ok, out = _run(["cargo", "clippy", "--offline", "--", "-D", "warnings"], work)
                    checks.append(_check("clippy warnings denied", ok, out))
            elif task_id.startswith("WEB-"):
                if task_id == "WEB-REF-01":
                    text = _without_comments_and_strings((work / "js/app.js").read_text())
                    declared = re.search(r"\bfunction\s+bindEvents\s*\(", text)
                    body, start, end = _body_after(text, declared)
                    outside = text[:declared.start()] + text[end:] if declared and end >= 0 else ""
                    calls = len(re.findall(r"\bbindEvents\s*\(", outside))
                    wiring = bool(declared and re.search(r"querySelector\s*\(", body) and re.search(r"addEventListener\s*\(", body))
                    ok = bool(declared) and calls >= 1 and wiring
                    checks.append(_check("bindEvents declaration, call, and event wiring", ok, f"calls={calls}, wiring={wiring}"))
                suite = {"WEB-BUG-01": "remaining.test.js", "WEB-FEAT-01": "clear.test.js", "WEB-REF-01": "todo.test.js"}[task_id]
                ok, out = _run(["node", "--test", f"tests/{suite}"], work)
                checks.append(_check("trusted Node tests", ok, out))
                if task_id == "WEB-REF-01":
                    env_old = os.environ.get("VERIFIER_FIXTURE")
                    os.environ["VERIFIER_FIXTURE"] = str(work)
                    try:
                        ok, out = _run(["node", "--test", str(VERIFIERS_DIR / "mini_web_ui.test.js")], work)
                    finally:
                        if env_old is None: os.environ.pop("VERIFIER_FIXTURE", None)
                        else: os.environ["VERIFIER_FIXTURE"] = env_old
                    checks.append(_check("trusted UI event regressions", ok, out))
            else:
                # Existing fixture tests are trusted copies; jsdom resolves from
                # the trusted fixture's lockfile install, never candidate files.
                suite = {"FEAT-02": "dark-mode.test.js", "FEAT-03": "search.test.js", "FEAT-04": "favorites.test.js",
                         "FEAT-05": "form-validation.test.js", "FEAT-06": "accessible-nav.test.js"}[task_id]
                ok, out = _run(["node", "--test", f"tests/{suite}"], work)
                checks.append(_check("trusted website tests", ok, out))
                if task_id == "FEAT-06":
                    ok, out = _browser_nav_check(work)
                    checks.append(_check("real Chromium keyboard focus trap", ok, out))
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
    passed = not errors and bool(checks) and all(c["passed"] for c in checks)
    result = {"task_id": task_id, "passed": passed, "checks": checks, "errors": errors}
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
