// Black-box test for a keyboard-accessible mobile nav (see
// tasks/.../feat-06-accessible-nav.yaml). jsdom does not implement native
// Tab-key focus movement the way a real browser does, so for any of this
// to work the site's own JS must explicitly listen for Tab/Shift+Tab and
// move focus itself with .focus() + preventDefault() - which is also the
// only reliable way to build a real focus trap in a real browser, so the
// test exercising it this way matches how a correct implementation would
// actually have to work, not just how jsdom happens to behave.
//
//   1. opening the nav (clicking #nav-toggle) moves focus to the first
//      link inside #site-nav
//   2. pressing Escape while open closes the nav and returns focus to
//      #nav-toggle
//   3. pressing Tab on the last focusable element in the open nav wraps
//      focus back to #nav-toggle (not out of the menu)
//   4. pressing Shift+Tab on #nav-toggle while open wraps focus to the
//      last link
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SITE_DIR = path.join(__dirname, "..");
const PAGE = "index.html";

let loadCounter = 0;

function loadPage() {
  loadCounter += 1;
  const htmlPath = path.join(SITE_DIR, PAGE);
  const html = fs.readFileSync(htmlPath, "utf8");
  const dom = new JSDOM(html, {
    url: `http://localhost-${loadCounter}.invalid/`,
    runScripts: "dangerously",
  });
  const scriptSrcs = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((m) => m[1]);
  for (const src of scriptSrcs) {
    const code = fs.readFileSync(path.join(SITE_DIR, src), "utf8");
    dom.window.eval(code);
  }
  return dom;
}

function tick(dom) {
  return new Promise((resolve) => dom.window.setTimeout(resolve, 10));
}

function keydown(dom, target, key, opts = {}) {
  target.dispatchEvent(
    new dom.window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...opts })
  );
}

function navLinks(document) {
  return [...document.querySelectorAll("#site-nav a")];
}

describe("accessible mobile nav", () => {
  it("moves focus to the first nav link when opened", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;

    document.getElementById("nav-toggle").click();
    await tick(dom);

    assert.equal(document.activeElement, navLinks(document)[0]);
  });

  it("Escape closes the nav and returns focus to the toggle", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const toggle = document.getElementById("nav-toggle");

    toggle.click();
    await tick(dom);
    keydown(dom, document, "Escape");
    await tick(dom);

    assert.equal(document.getElementById("site-nav").classList.contains("open"), false);
    assert.equal(toggle.getAttribute("aria-expanded"), "false");
    assert.equal(document.activeElement, toggle);
  });

  it("Tab on the last link wraps focus back to the toggle", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const toggle = document.getElementById("nav-toggle");

    toggle.click();
    await tick(dom);
    const links = navLinks(document);
    links[links.length - 1].focus();

    keydown(dom, links[links.length - 1], "Tab");
    await tick(dom);

    assert.equal(document.activeElement, toggle);
  });

  it("Shift+Tab on the toggle wraps focus to the last link", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const toggle = document.getElementById("nav-toggle");

    toggle.click();
    await tick(dom);
    toggle.focus();

    keydown(dom, toggle, "Tab", { shiftKey: true });
    await tick(dom);

    const links = navLinks(document);
    assert.equal(document.activeElement, links[links.length - 1]);
  });
});
