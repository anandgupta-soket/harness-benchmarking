// Black-box dark-mode test: doesn't care how the site implements it
// internally, only checks the observable contract the task prompt asks
// for (see tasks/.../feat-02-dark-mode.yaml):
//
//   1. a clickable element with id="theme-toggle"
//   2. clicking it sets document.documentElement.dataset.theme = "dark"
//      (and toggling again sets it back to "light")
//   3. the choice is written to localStorage under the key "theme"
//   4. a fresh page load with "theme" already in localStorage starts
//      with that theme already applied (real persistence-across-reload,
//      simulated via jsdom's beforeParse hook pre-seeding storage before
//      the page's own scripts run - the standard way to test this
//      without an actual browser/second process)
//
// The site is multi-page (index/about/services/contact.html), so the
// contract is checked on every *.html page in the fixture, not just
// index.html - a harness that only wires up dark mode on the homepage and
// leaves the rest of the site unthemed should fail this test, since
// requirement 5 ("all existing page content and functionality must keep
// working") implies the toggle works sitewide, not on one page.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SITE_DIR = path.join(__dirname, "..");

const PAGES = fs
  .readdirSync(SITE_DIR)
  .filter((name) => name.endsWith(".html"))
  .sort();

// localStorage only works under a real http(s) origin in jsdom, not file://
// (confirmed directly: window.localStorage was undefined without this).
// But giving JSDOM that http:// url also makes it try to *fetch* every
// relative <script src>/<link href> over real HTTP against that fake
// origin (confirmed too: ECONNREFUSED on 127.0.0.1:80). So: don't let jsdom
// auto-load external resources at all, and instead read each <script src>
// file ourselves and eval() its content directly into the window - gets a
// working localStorage AND the site's real JS actually running, without
// needing an actual server.
let loadCounter = 0;

function loadPage(page, { seedTheme } = {}) {
  // Unique origin per call: jsdom's localStorage is keyed by origin and
  // confirmed shared across separate JSDOM instances using the same origin
  // string within one process - node:test runs sibling `it()`s
  // concurrently by default, so a fixed "http://localhost/" let tests
  // clobber each other's localStorage (one test's click would be seen as
  // already-dark by another, failing with the wrong toggle direction).
  loadCounter += 1;
  const htmlPath = path.join(SITE_DIR, page);
  const html = fs.readFileSync(htmlPath, "utf8");
  const dom = new JSDOM(html, {
    url: `http://localhost-${loadCounter}.invalid/`,
    runScripts: "dangerously",
    beforeParse(window) {
      if (seedTheme) {
        window.localStorage.setItem("theme", seedTheme);
      }
    },
  });

  const scriptSrcs = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((m) => m[1]);
  for (const src of scriptSrcs) {
    const code = fs.readFileSync(path.join(SITE_DIR, src), "utf8");
    dom.window.eval(code);
  }
  // No manual DOMContentLoaded dispatch: a synchronously-constructed
  // `new JSDOM(..., {runScripts: "dangerously"})` already fires a real one
  // once parsing/scripts finish. Dispatching a second one here attached
  // every DOMContentLoaded-based listener (the toggle's click handler)
  // twice, so a single click silently toggled twice - net no-op - and
  // masked the bug it should have caught.
  return dom;
}

function tick(dom) {
  return new Promise((resolve) => dom.window.setTimeout(resolve, 10));
}

for (const page of PAGES) {
  describe(`dark mode (${page})`, () => {
    it("has a #theme-toggle element", async () => {
      const dom = loadPage(page);
      await tick(dom);
      const toggle = dom.window.document.getElementById("theme-toggle");
      assert.ok(toggle, `expected an element with id="theme-toggle" on ${page}`);
    });

    it("clicking the toggle switches to dark and writes localStorage", async () => {
      const dom = loadPage(page);
      await tick(dom);
      const { document, localStorage } = dom.window;
      const toggle = document.getElementById("theme-toggle");
      assert.ok(toggle, `expected an element with id="theme-toggle" on ${page}`);

      toggle.click();
      await tick(dom);

      assert.equal(document.documentElement.dataset.theme, "dark");
      assert.equal(localStorage.getItem("theme"), "dark");
    });

    it("clicking twice toggles back to light", async () => {
      const dom = loadPage(page);
      await tick(dom);
      const { document } = dom.window;
      const toggle = document.getElementById("theme-toggle");

      toggle.click();
      await tick(dom);
      toggle.click();
      await tick(dom);

      assert.equal(document.documentElement.dataset.theme, "light");
    });

    it("a fresh load respects a previously-stored dark preference", async () => {
      const dom = loadPage(page, { seedTheme: "dark" });
      await tick(dom);
      assert.equal(
        dom.window.document.documentElement.dataset.theme,
        "dark",
        `${page} should apply the stored theme on load, without needing another click`
      );
    });
  });
}
