#!/usr/bin/env python3
"""Build the three-page harness benchmark report (PDF) from runs/runs.csv and
runs/suites.csv.

  ~/.cache/harness-report-venv/bin/python scripts/make_report.py

One-time setup (isolated, nothing touches the system Python):
  python3 -m venv ~/.cache/harness-report-venv
  ~/.cache/harness-report-venv/bin/pip install reportlab

Page 1: model, harnesses and metric charts.
Page 2: the test cases used (the 13 tasks and how each is graded).
Page 3: the detailed run log, one line per task per harness.

Charts cover harnesses that ran the whole suite; a harness with only some
tasks run (DeepSeek Harness, paused) appears on page 3 with what it did run.
For each harness x task the *latest* logged run is used, so re-running after
new results land refreshes everything.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from run_task import ROOT
from validate_tasks import load_suite_index, parse_simple_yaml

W, H = 612, 792  # US Letter, points
MX = 54  # side margin

INK, INK2, MUTE, HAIR = "#1d1d1f", "#6e6e73", "#86868b", "#e5e5ea"
GOOD, BAD = "#1c8a3a", "#c9352b"

# id -> (display name, maker, version, colour)
HARNESSES = {
    "loop": ("Loop", "Soket AI", "0.3.1", "#2a78d6"),
    "opencode": ("OpenCode", "opencode.ai", "2.0.20", "#eb6834"),
    "pi": ("Pi", "earendil-works", "1.0.3", "#1baf7a"),
    "deepseek-harness": ("DeepSeek Harness", "DeepSeek AI", "0.1.5rc1", "#eda100"),
    "goose": ("Goose", "Block / AAIF", "1.53.0", "#e87ba4"),
}
PRICE_IN, PRICE_OUT = 0.15e-6, 0.50e-6  # USD/token, z-ai/glm-5.3-flash on OpenRouter

# task id -> (what the agent is asked, how it is graded)
TESTS = {
    "FEAT-02": ("Add a light/dark toggle (#theme-toggle) to all four pages that remembers the choice after reload.",
                "16 checks (4 per page): toggle present, click saves dark, click again light, reload restores."),
    "FEAT-03": ("Add a live search box on the Services page that hides cards that don't match what you type.",
                "4 checks: input present, filters cards, case-insensitive incl. body text, clearing restores all."),
    "FEAT-04": ("Add a save button per service card, persist it across reloads and show a live count in the header.",
                "4 checks: count starts at 0, click saves + updates count, click again removes, fresh load restores state."),
    "FEAT-05": ("Make the contact form validate: required name and message, real email format, inline errors.",
                "3 checks: invalid blocked with errors, valid clears errors and shows thanks, bad email rejected."),
    "FEAT-06": ("Make the mobile nav keyboard-accessible with a real focus trap.",
                "4 checks: focus enters menu, Escape closes and returns focus, Tab/Shift+Tab wrap inside it."),
    "WEB-BUG-01": ("Fix remainingCount() so it counts unfinished to-dos, not all of them.",
                   "node --test tests/remaining.test.js (3 checks)."),
    "WEB-FEAT-01": ("Implement clearCompleted() so it removes finished to-dos.",
                    "node --test tests/clear.test.js (3 checks)."),
    "WEB-REF-01": ("Extract the repeated event wiring into one function named bindEvents, behavior unchanged.",
                   "Script: the existing tests still pass and `function bindEvents(` exists."),
    "BUG-01": ("Fix the broken add() function (returns a - b).",
               "cargo test: add_works, mul_works, is_even_works must all pass."),
    "FEAT-01": ("Make parse_verbose_flag accept --verbose as well as -v.",
                "Text check: src/lib.rs contains \"--verbose\"."),
    "REF-01": ("Pull the duplicated number parsing into a helper named parse_i32_arg.",
               "Text check: src/main.rs contains \"fn parse_i32_arg\"."),
    "TOOL-01": ("Fix add() and then clear every Rust linter (clippy) warning.",
                "cargo test and cargo clippy -D warnings must both succeed."),
    "AGENT-01": ("Find the failing test without being told which, fix it, and re-run to confirm.",
                 "cargo test --quiet passes."),
}
GROUPS = [
    ("Aurora Studio website  ·  4 static pages", ["FEAT-02", "FEAT-03", "FEAT-04", "FEAT-05", "FEAT-06"]),
    ("To-do app  ·  JavaScript", ["WEB-BUG-01", "WEB-FEAT-01", "WEB-REF-01"]),
    ("Calculator  ·  Rust", ["BUG-01", "FEAT-01", "REF-01", "TOOL-01", "AGENT-01"]),
]


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def fmt_tok(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}k" if n >= 10_000 else f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def load():
    tasks = []
    for path in load_suite_index():
        if path.is_file():
            d = parse_simple_yaml(path.read_text(encoding="utf-8"))
            tasks.append((str(d.get("id", "")), str(d.get("title", ""))))
    ids = [t for t, _ in tasks]
    latest = {}
    with (ROOT / "runs" / "runs.csv").open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["task_id"] in ids:
                latest[(r["harness"], r["task_id"])] = r
    suites = []
    sp = ROOT / "runs" / "suites.csv"
    if sp.is_file():
        with sp.open(newline="", encoding="utf-8") as fh:
            suites = list(csv.DictReader(fh))
    present = [h for h in HARNESSES if any((h, t) in latest for t in ids)]
    complete = [h for h in present if all((h, t) in latest for t in ids)]
    stats = {}
    for h in present:
        rows = [latest[(h, t)] for t in ids if (h, t) in latest]
        wall = [num(r["wall_clock_ms"]) for r in rows if num(r["wall_clock_ms"]) is not None]
        suite = [s for s in suites if s.get("harness") == h and s.get("tasks_run") == s.get("tasks_total") == str(len(ids))]
        stats[h] = {
            "n": len(rows),
            "pass": sum(r["success"].strip().lower() == "true" for r in rows),
            "suite_time": float(suite[-1]["total_wall_s"]) if suite else sum(wall) / 1000,
            "tokens": sum(num(r["total_tokens"]) or 0 for r in rows),
            "tools": sum(num(r["tool_calls"]) or 0 for r in rows),
            "cost": sum((num(r["input_tokens"]) or 0) * PRICE_IN + (num(r["output_tokens"]) or 0) * PRICE_OUT for r in rows),
            "ram": statistics.median([(num(r["peak_rss_kb"]) or 0) / 1024 for r in rows if num(r["peak_rss_kb"])] or [0]),
            "cpu": statistics.median([(num(r["cpu_user_s"]) or 0) + (num(r["cpu_sys_s"]) or 0) for r in rows if num(r["cpu_user_s"]) is not None] or [0]),
            "med_time": statistics.median(wall) / 1000 if wall else 0.0,
        }
    return ids, dict(tasks), latest, present, complete, stats


# ------------------------------------------------------------------ drawing helpers
class Pen:
    def __init__(self, c: canvas.Canvas):
        self.c = c

    def text(self, x, top, s, font="Helvetica", size=10, color=INK, anchor="l"):
        self.c.setFillColor(HexColor(color))
        self.c.setFont(font, size)
        y = H - top
        {"l": self.c.drawString, "r": self.c.drawRightString, "c": self.c.drawCentredString}[anchor](x, y, s)

    def wrapped(self, x, top, s, width, font="Helvetica", size=9, color=INK2, leading=None) -> float:
        leading = leading or size * 1.32
        for i, line in enumerate(simpleSplit(s, font, size, width)):
            self.text(x, top + i * leading, line, font, size, color)
        return top + len(simpleSplit(s, font, size, width)) * leading

    def hline(self, x1, x2, top, color=HAIR, width=0.6):
        self.c.setStrokeColor(HexColor(color))
        self.c.setLineWidth(width)
        self.c.line(x1, H - top, x2, H - top)

    def vline(self, x, top1, top2, color=HAIR, width=0.6):
        self.c.setStrokeColor(HexColor(color))
        self.c.setLineWidth(width)
        self.c.line(x, H - top1, x, H - top2)

    def dot(self, x, top, r, color):
        self.c.setFillColor(HexColor(color))
        self.c.circle(x, H - top, r, stroke=0, fill=1)

    def bar(self, x, top, w, h, color, r=3.5):
        """Horizontal bar: square at the baseline (left), rounded at the tip."""
        self.c.setFillColor(HexColor(color))
        y = H - top - h
        self.c.roundRect(x, y, w, h, min(r, w / 2), stroke=0, fill=1)
        self.c.rect(x, y, min(w, r + 1), h, stroke=0, fill=1)


def footer(p: Pen, n: int, note: str):
    p.hline(MX, W - MX, H - 46)
    for i, line in enumerate(simpleSplit(note, "Helvetica", 6.8, W - 2 * MX - 50)[:2]):
        p.text(MX, H - 34 + i * 8.5, line, size=6.8, color=MUTE)
    p.text(W - MX, H - 34, f"{n} / 3", size=7, color=MUTE, anchor="r")


def bar_chart(p: Pen, x, top, width, title, note, items, fmt):
    p.text(x, top + 9, title, "Helvetica-Bold", 10.5, INK)
    p.text(x, top + 20, note, size=7.2, color=MUTE)
    label_w, bar_h, row_h = 70, 10, 20
    max_w = width - label_w - 54
    peak = max((v for _, _, v, _ in items), default=0) or 1
    y0 = top + 29
    p.vline(x + label_w, y0 - 3, y0 + len(items) * row_h - 7)
    for i, (_, name, value, color) in enumerate(items):
        y = y0 + i * row_h
        w = max(3, value / peak * max_w)
        p.text(x + label_w - 8, y + bar_h - 2.5, name, size=8.4, color=INK2, anchor="r")
        p.bar(x + label_w, y, w, bar_h, color)
        p.text(x + label_w + w + 6, y + bar_h - 2.5, fmt(value), "Helvetica-Bold", 8.6, INK)


# ------------------------------------------------------------------ pages
def page1(p, ids, present, complete, stats):
    p.text(MX, 68, "CODING-AGENT HARNESS BENCHMARK", "Helvetica-Bold", 8, MUTE)
    p.text(MX, 112, "Same model.", "Helvetica-Bold", 31, INK)
    p.text(MX, 148, "Same tasks. Different harness.", "Helvetica-Bold", 31, INK)
    p.wrapped(MX, 172, f"Each harness got the identical {len(ids)} small coding tasks and was graded automatically by running "
              "real tests. Tasks ran one after another on one laptop, so the numbers are directly comparable.",
              430, size=10, color=INK2, leading=14)

    top = 214
    p.hline(MX, W - MX, top)
    facts = [("MODEL", "GLM 5.3 Flash", "z-ai/glm-5.3-flash via OpenRouter"),
             ("HARNESSES", str(len(present)), f"{len(complete)} full runs, {len(present) - len(complete)} partial"),
             ("TASKS", str(len(ids)), "website, JavaScript, Rust"),
             ("DATE", datetime.now().strftime("%b %-d, %Y"), "one run per task")]
    cw = (W - 2 * MX) / len(facts)
    for i, (k, v, s) in enumerate(facts):
        x = MX + i * cw + (0 if i == 0 else 14)
        if i:
            p.vline(MX + i * cw, top + 8, top + 56)
        p.text(x, top + 22, k, "Helvetica-Bold", 6.8, MUTE)
        p.text(x, top + 40, v, "Helvetica-Bold", 14 if i != 3 else 12, INK)
        p.text(x, top + 52, s, size=6.6, color=INK2)
    p.hline(MX, W - MX, top + 64)

    def items(key, scale=1.0):
        return [(h, HARNESSES[h][0], stats[h][key] * scale, HARNESSES[h][3]) for h in complete]

    charts = [
        ("Pass rate", "tasks passed out of 13 · higher is better",
         [(h, HARNESSES[h][0], stats[h]["pass"], HARNESSES[h][3]) for h in complete], lambda v: f"{v:.0f} / {len(ids)}"),
        ("Total run time", "13 tasks, back to back · lower is better", items("suite_time", 1 / 60), lambda v: f"{v:.1f} min"),
        ("Tokens used", "all tasks combined · lower is better", items("tokens"), fmt_tok),
        ("Estimated cost", "list pricing, upper bound · lower is better", items("cost"), lambda v: f"${v:.2f}"),
        ("Tool calls", "all tasks combined · fewer is leaner", items("tools"), lambda v: f"{v:.0f}"),
        ("Median task time", "typical task · lower is better", items("med_time"), lambda v: f"{v:.0f} s"),
        ("Peak memory", "median per task · lower is better", items("ram"), lambda v: f"{v:.0f} MB"),
        ("CPU time", "median per task, user + system", items("cpu"), lambda v: f"{v:.1f} s"),
    ]
    colw = (W - 2 * MX - 34) / 2
    gy = top + 84
    for i, (title, note, its, fmt) in enumerate(charts):
        bar_chart(p, MX + (i % 2) * (colw + 34), gy + (i // 2) * 112, colw, title, note, its, fmt)
    footer(p, 1, "One run per task per harness, so small gaps are within noise. Cost is an upper-bound estimate from list pricing. "
                 "DeepSeek Harness is paused partway and shown on page 3 only.")


def page2(p, ids, titles):
    p.text(MX, 68, "THE TEST CASES", "Helvetica-Bold", 8, MUTE)
    p.text(MX, 98, "13 tasks, graded by real tests", "Helvetica-Bold", 22, INK)
    p.wrapped(MX, 114, "Every task starts from a small project with a known problem. The agent works on its own copy; afterwards an "
              "automated check decides pass or fail. No human scoring and no AI judge.", W - 2 * MX, size=9, color=INK2, leading=12.5)
    y = 142
    for gname, gids in GROUPS:
        p.text(MX, y + 8, gname.upper(), "Helvetica-Bold", 7.2, MUTE)
        p.hline(MX, W - MX, y + 13)
        y += 19
        for tid in gids:
            ask, grade = TESTS[tid]
            p.text(MX, y + 8, tid, "Helvetica-Bold", 9, INK)
            p.text(MX + 72, y + 8, titles.get(tid, ""), "Helvetica-Bold", 9, INK)
            nxt = p.wrapped(MX + 72, y + 18.5, ask, W - 2 * MX - 72, size=7.9, color=INK2, leading=9.4)
            nxt = p.wrapped(MX + 72, nxt + 0.5, "Graded by: " + grade, W - 2 * MX - 72, size=7.3, color=MUTE, leading=9)
            y = nxt + 2.5
        y += 3
    footer(p, 2, "Aurora Studio and to-do tests simulate a browser (jsdom); Rust tasks use cargo test and clippy.")


def page3(p, ids, latest, present, complete, stats):
    p.text(MX, 68, "THE RUN LOG", "Helvetica-Bold", 8, MUTE)
    p.text(MX, 98, "Every run, task by task", "Helvetica-Bold", 22, INK)
    p.wrapped(MX, 114, "One line per task: position in the suite, task, result, time the agent worked, tokens and tool calls.",
              W - 2 * MX, size=9, color=INK2)

    order = [h for h in ("loop", "pi", "goose", "opencode", "deepseek-harness") if h in present]
    colw = (W - 2 * MX - 28) / 2
    pos = {}

    def block(h, x, top) -> float:
        name, maker, ver, color = HARNESSES[h]
        s = stats[h]
        p.dot(x + 3.5, top + 5, 3.5, color)
        p.text(x + 12, top + 8, name, "Helvetica-Bold", 10, INK)
        done = s["n"] == len(ids)
        summary = (f'{s["pass"]}/{s["n"]} passed · {s["suite_time"] / 60:.1f} min' if done
                   else f'{s["pass"]}/{s["n"]} passed · partial, {len(ids) - s["n"]} not run')
        p.text(x + colw, top + 8, summary, size=7.6, color=INK2, anchor="r")
        p.hline(x, x + colw, top + 13)
        y = top + 24
        for i, t in enumerate(ids, start=1):
            r = latest.get((h, t))
            if not r:
                line = f"[{i:>2}/{len(ids)}] {t:<11} not run"
                p.text(x, y, line, "Courier", 6.9, MUTE)
            else:
                ok = r["success"].strip().lower() == "true"
                secs = (num(r["wall_clock_ms"]) or 0) / 1000
                tok = num(r["total_tokens"]) or 0
                tools = num(r["tool_calls"])
                p.text(x, y, f"[{i:>2}/{len(ids)}] {t:<11}", "Courier", 6.9, INK)
                p.text(x + 4.14 * 20, y, "PASS" if ok else "FAIL", "Courier-Bold", 6.9, GOOD if ok else BAD)
                p.text(x + 4.14 * 25, y, f"{secs:>6.1f}s {fmt_tok(tok):>6} tok {('%d' % tools) if tools is not None else '-':>3} tools",
                       "Courier", 6.9, INK2)
            y += 10.2
        return y

    ys = []
    for idx, h in enumerate(order[:4]):
        col, row = idx % 2, idx // 2
        x = MX + col * (colw + 28)
        top = 150 + row * 188
        ys.append(block(h, x, top))
    # third row: DeepSeek (partial) left, notes right
    top3 = 150 + 2 * 188
    if len(order) > 4:
        block(order[4], MX, top3)
    nx = MX + colw + 28
    p.text(nx, top3 + 8, "Reading the log", "Helvetica-Bold", 10, INK)
    p.hline(nx, nx + colw, top3 + 13)
    notes = [
        "Each line is one task, in suite order, with the latest logged run.",
        "Time is the agent's own working time on that task.",
        "Goose's two FAILs ended within seconds (3 and 6 s of agent time) after a single tool call, which looks like an early exit rather than a hard task; worth rerunning.",
        "DeepSeek Harness ran 3 tasks and was paused after a task hung on a command that never returned.",
    ]
    yy = top3 + 25
    for n in notes:
        yy = p.wrapped(nx, yy, "· " + n, colw, size=7.8, color=INK2, leading=10.2) + 3
    footer(p, 3, "PASS means the task's automated check succeeded. All runs used z-ai/glm-5.3-flash on OpenRouter.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "harness-benchmark-report.pdf"))
    args = ap.parse_args()
    ids, titles, latest, present, complete, stats = load()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out), pagesize=(W, H))
    c.setTitle("Coding-agent harness benchmark")
    p = Pen(c)
    page1(p, ids, present, complete, stats)
    c.showPage()
    page2(p, ids, titles)
    c.showPage()
    page3(p, ids, latest, present, complete, stats)
    c.showPage()
    c.save()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
