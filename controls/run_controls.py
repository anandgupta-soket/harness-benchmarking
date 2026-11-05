"""Execute checked-in positive and negative controls for all thirteen tasks.

Run with ``python controls/run_controls.py``. Controls are made from pristine
fixtures and pass through the same real verifier used for benchmark results.
"""
from __future__ import annotations

import json
import stat
import shutil
import tempfile
from pathlib import Path

from bench.paths import FIXTURES_DIR
from bench.tasks import TASK_IDS
from bench.verification import _ALLOW, _FIXTURE, verify

ROOT = Path(__file__).resolve().parents[1]


def _make_temp_tree_writable(root: Path) -> None:
    """Restore owner write permission on copies of the read-only image fixtures.

    The image deliberately removes write bits from trusted fixture sources.
    ``copytree`` preserves those mode bits, while the controls job drops all
    capabilities; make only its private temporary copies writable for positive
    edits and verifier build outputs.
    """
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            mode |= stat.S_IWUSR | stat.S_IXUSR
        else:
            mode |= stat.S_IWUSR
        path.chmod(mode)


def _site_solution(base: Path) -> None:
    # Add controls to all pages and interactive page controls where relevant.
    for page in base.glob("*.html"):
        html = page.read_text()
        brand_end = html.find("</a>", html.find('class="brand"')) + 4
        html = html[:brand_end] + '\n    <button id="theme-toggle" type="button">Theme</button>' + html[brand_end:]
        if page.name == "services.html":
            html = html.replace('<section class="card-grid" id="service-cards">', '<input id="service-search" type="search" />\n    <section class="card-grid" id="service-cards">')
            html = html.replace('class="card" data-id="', 'class="card" data-id="')
            for cid in ("new-site", "redesign", "maintenance"):
                html = html.replace(f'data-id="{cid}">', f'data-id="{cid}"><button class="favorite-toggle" type="button" aria-pressed="false">Favorite</button>')
            html = html.replace('<header class="site-header">', '<header class="site-header"><span id="favorites-count">0</span>')
        if page.name == "contact.html":
            for name in ("name", "email", "message"):
                token = f'id="{name}"'
                end = html.find(" />", html.find(token))
                if end < 0:
                    end = html.find("</textarea>", html.find(token)) + len("</textarea>")
                    html = html[:end] + f'<p id="{name}-error"></p>' + html[end:]
                else:
                    end += 3
                    html = html[:end] + f'<p id="{name}-error"></p>' + html[end:]
        page.write_text(html)
    app = r'''(function () {
  document.addEventListener("DOMContentLoaded", function () {
    const year = document.getElementById("year"); if (year) year.textContent = String(new Date().getFullYear());
    const theme = localStorage.getItem("theme"); if (theme) document.documentElement.dataset.theme = theme;
    const toggleTheme = document.getElementById("theme-toggle");
    if (toggleTheme) toggleTheme.addEventListener("click", function () { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("theme", next); });
    const search = document.getElementById("service-search");
    if (search) search.addEventListener("input", function () { const q = search.value.toLowerCase(); document.querySelectorAll("#service-cards .card").forEach(c => { c.hidden = !c.textContent.toLowerCase().includes(q); }); });
    const cards = document.querySelectorAll("#service-cards .card");
    if (cards.length) {
      let favs; try { favs = JSON.parse(localStorage.getItem("favorites") || "[]"); if (!Array.isArray(favs)) favs = []; } catch (_) { favs = []; }
      const update = () => { document.querySelectorAll("#service-cards .card").forEach(c => { const b = c.querySelector(".favorite-toggle"); if (b) b.setAttribute("aria-pressed", String(favs.includes(c.dataset.id))); }); const count = document.getElementById("favorites-count"); if (count) count.textContent = String(favs.length); };
      update(); document.querySelectorAll("#service-cards .favorite-toggle").forEach(b => b.addEventListener("click", function () { const id = b.closest(".card").dataset.id; favs = favs.includes(id) ? favs.filter(x => x !== id) : favs.concat(id); localStorage.setItem("favorites", JSON.stringify(favs)); update(); }));
    }
    const form = document.getElementById("contact-form");
    if (form) form.addEventListener("submit", function (e) { e.preventDefault(); const values = {name: document.getElementById("name").value.trim(), email: document.getElementById("email").value.trim(), message: document.getElementById("message").value.trim()}; const bad = {name: !values.name, email: !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email), message: !values.message}; let invalid = false; for (const key of Object.keys(bad)) { const el = document.getElementById(key + "-error"); if (el) el.textContent = bad[key] ? "Please enter a valid " + key + "." : ""; invalid = invalid || bad[key]; } const status = document.getElementById("form-status"); if (!invalid && status) status.textContent = "Thanks! This demo form doesn't actually send anything."; else if (invalid && status) status.textContent = ""; });
    const nav = document.getElementById("site-nav"), button = document.getElementById("nav-toggle");
    if (nav && button) { const links = () => [...nav.querySelectorAll("a")]; const close = () => { nav.classList.remove("open"); button.setAttribute("aria-expanded", "false"); button.focus(); }; button.addEventListener("click", function () { const open = nav.classList.toggle("open"); button.setAttribute("aria-expanded", String(open)); if (open && links()[0]) links()[0].focus(); }); document.addEventListener("keydown", function (e) { if (!nav.classList.contains("open")) return; const a = links(); if (e.key === "Escape") { close(); } else if (e.key === "Tab" && !e.shiftKey && a.length && document.activeElement === a[a.length - 1]) { e.preventDefault(); button.focus(); } else if (e.key === "Tab" && e.shiftKey && document.activeElement === button && a.length) { e.preventDefault(); a[a.length - 1].focus(); } }); }
  });
})();
'''
    (base / "js/app.js").write_text(app)


def _solution(task: str, candidate: Path) -> None:
    fixture = FIXTURES_DIR / _FIXTURE[task]
    shutil.copytree(fixture, candidate, ignore=shutil.ignore_patterns("node_modules", "target", ".git"))
    _make_temp_tree_writable(candidate)
    if task in ("BUG-01", "AGENT-01", "TOOL-01"):
        p = candidate / "src/lib.rs"
        s = p.read_text().replace("a - b", "a + b").replace('''    if n % 2 == 0 {
        return true;
    } else {
        return false;
    }''', "    n % 2 == 0")
        s = s.replace('args.iter().any(|a| a == "-v")', 'args.iter().any(|a| a == "-v" || a == "--verbose")')
        p.write_text(s)
    elif task == "FEAT-01":
        p = candidate / "src/lib.rs"
        p.write_text(p.read_text().replace('a == "-v"', 'a == "-v" || a == "--verbose"'))
    elif task == "REF-01":
        p = candidate / "src/main.rs"
        s = p.read_text().replace('''    let a: i32 = args[2].parse().expect("a must be integer");
    let b: i32 = args[3].parse().expect("b must be integer");''', '''    let a = parse_i32_arg(&args[2], "a");
    let b = parse_i32_arg(&args[3], "b");''')
        s = 'fn parse_i32_arg(value: &str, label: &str) -> i32 { value.parse().unwrap_or_else(|_| panic!("{} must be integer", label)) }\n\n' + s
        p.write_text(s)
    elif task.startswith("WEB-"):
        p = candidate / "js/todo.js"
        s = p.read_text()
        if task == "WEB-BUG-01":
            s = s.replace("return todos.length;", "return todos.filter((todo) => !todo.completed).length;")
        if task == "WEB-FEAT-01":
            s = s.replace("return todos.slice();\n}\n\nconst MiniTodo", "return todos.filter((todo) => !todo.completed);\n}\n\nconst MiniTodo")
        p.write_text(s)
        if task == "WEB-REF-01":
            p = candidate / "js/app.js"
            s = p.read_text()
            # Move all delegated click/submit wiring into a live function.
            start = s.index('  document.querySelector("#add-form")')
            end = s.index("\n  render();", start)
            wiring = s[start:end]
            # bindEvents must be used and contain all querySelector/listener wiring.
            s = s[:start] + "  function bindEvents() {\n" + wiring + "\n  }\n\n  bindEvents();" + s[end + len("\n  render();"):]
            # Keep initial render separate.
            s = s.replace("  bindEvents();\n})();", "  bindEvents();\n  render();\n})();")
            p.write_text(s)
    else:
        _site_solution(candidate)


def main() -> int:
    summaries = {}
    with tempfile.TemporaryDirectory(prefix="loop-bench-controls-") as td:
        tmp = Path(td)
        for task in TASK_IDS:
            candidate = tmp / "positive" / task
            candidate.parent.mkdir(parents=True, exist_ok=True)
            _solution(task, candidate)
            positive = verify(task, candidate, tmp / "out" / "positive" / task)
            # A pristine checked-in fixture is the corresponding negative
            # control, run through the same grader from a separate directory.
            negative_dir = tmp / "negative" / task
            negative_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(FIXTURES_DIR / _FIXTURE[task], negative_dir,
                            ignore=shutil.ignore_patterns("node_modules", "target", ".git"))
            _make_temp_tree_writable(negative_dir)
            negative = verify(task, negative_dir, tmp / "out" / "negative" / task)
            summaries[task] = {"positive": positive["passed"], "negative_rejected": not negative["passed"],
                               "failed_checks": [c for c in positive["checks"] if not c["passed"]],
                               "positive_errors": positive["errors"], "negative_errors": negative["errors"]}
    print(json.dumps(summaries, indent=2))
    return 0 if all(v["positive"] and v["negative_rejected"] for v in summaries.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
