// Black-box test for the favorites feature on services.html (see
// tasks/.../feat-04-favorites.yaml):
//
//   1. each .card has a button with class "favorite-toggle"
//   2. clicking it toggles that card's data-id in localStorage["favorites"]
//      (a JSON array) and flips its own aria-pressed attribute
//   3. a #favorites-count element reflects the live count, updated
//      immediately on toggle
//   4. a fresh load with favorites already in localStorage shows the
//      correct count AND the right cards' toggles already aria-pressed,
//      without any click - real persistence, not just a write nobody reads
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SITE_DIR = path.join(__dirname, "..");
const PAGE = "services.html";

let loadCounter = 0;

function loadPage({ seedFavorites } = {}) {
  loadCounter += 1;
  const htmlPath = path.join(SITE_DIR, PAGE);
  const html = fs.readFileSync(htmlPath, "utf8");
  const dom = new JSDOM(html, {
    url: `http://localhost-${loadCounter}.invalid/`,
    runScripts: "dangerously",
    beforeParse(window) {
      if (seedFavorites) {
        window.localStorage.setItem("favorites", JSON.stringify(seedFavorites));
      }
    },
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

function toggleFor(document, id) {
  const card = document.querySelector(`.card[data-id="${id}"]`);
  assert.ok(card, `expected a .card with data-id="${id}"`);
  const toggle = card.querySelector(".favorite-toggle");
  assert.ok(toggle, `expected a .favorite-toggle button inside the "${id}" card`);
  return toggle;
}

describe("services favorites", () => {
  it("starts with a count of 0 and no card marked favorited", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const count = document.getElementById("favorites-count");
    assert.ok(count, 'expected an element with id="favorites-count"');
    assert.equal(count.textContent.trim(), "0");
    assert.equal(toggleFor(document, "new-site").getAttribute("aria-pressed"), "false");
  });

  it("clicking a card's toggle favorites it and updates the count", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document, localStorage } = dom.window;

    toggleFor(document, "new-site").click();
    await tick(dom);

    assert.equal(toggleFor(document, "new-site").getAttribute("aria-pressed"), "true");
    assert.equal(document.getElementById("favorites-count").textContent.trim(), "1");
    assert.deepEqual(JSON.parse(localStorage.getItem("favorites")), ["new-site"]);
  });

  it("clicking again unfavorites it", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;

    const toggle = toggleFor(document, "new-site");
    toggle.click();
    await tick(dom);
    toggle.click();
    await tick(dom);

    assert.equal(toggle.getAttribute("aria-pressed"), "false");
    assert.equal(document.getElementById("favorites-count").textContent.trim(), "0");
  });

  it("a fresh load respects previously-saved favorites", async () => {
    const dom = loadPage({ seedFavorites: ["redesign"] });
    await tick(dom);
    const { document } = dom.window;

    assert.equal(document.getElementById("favorites-count").textContent.trim(), "1");
    assert.equal(toggleFor(document, "redesign").getAttribute("aria-pressed"), "true");
    assert.equal(toggleFor(document, "new-site").getAttribute("aria-pressed"), "false");
  });
});
