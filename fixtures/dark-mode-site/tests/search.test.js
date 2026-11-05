// Black-box test for the search/filter feature on services.html (see
// tasks/.../feat-03-search-filter.yaml):
//
//   1. an <input id="service-search"> exists on services.html
//   2. typing a query hides (sets the `hidden` boolean attribute on) any
//      .card inside #service-cards whose text doesn't match, case-
//      insensitively, and un-hides ones that do
//   3. clearing the query shows every card again
//
// The three cards already carry a stable data-id (new-site/redesign/
// maintenance, see services.html) so the test can assert on specific cards
// rather than just "some card" - this fixture detail is prep, not part of
// what the harness has to invent.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SITE_DIR = path.join(__dirname, "..");
const PAGE = "services.html";

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

function setValue(dom, input, value) {
  const proto = Object.getPrototypeOf(input);
  Object.getOwnPropertyDescriptor(proto, "value").set.call(input, value);
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

function cardHidden(document, id) {
  const card = document.querySelector(`.card[data-id="${id}"]`);
  assert.ok(card, `expected a .card with data-id="${id}"`);
  return card.hidden === true;
}

describe("services search/filter", () => {
  it("has a #service-search input", async () => {
    const dom = loadPage();
    await tick(dom);
    const input = dom.window.document.getElementById("service-search");
    assert.ok(input, 'expected an element with id="service-search"');
  });

  it("filters to matching cards only", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const input = document.getElementById("service-search");
    assert.ok(input);

    setValue(dom, input, "redesign");
    await tick(dom);

    assert.equal(cardHidden(document, "redesign"), false, "redesign card should stay visible");
    assert.equal(cardHidden(document, "new-site"), true, "new-site card should be hidden");
    assert.equal(cardHidden(document, "maintenance"), true, "maintenance card should be hidden");
  });

  it("match is case-insensitive and matches body text too", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const input = document.getElementById("service-search");

    setValue(dom, input, "HOUR");
    await tick(dom);

    assert.equal(cardHidden(document, "maintenance"), false, "maintenance card (billed by the hour) should match 'hour'");
  });

  it("clearing the query shows every card again", async () => {
    const dom = loadPage();
    await tick(dom);
    const { document } = dom.window;
    const input = document.getElementById("service-search");

    setValue(dom, input, "redesign");
    await tick(dom);
    setValue(dom, input, "");
    await tick(dom);

    assert.equal(cardHidden(document, "new-site"), false);
    assert.equal(cardHidden(document, "redesign"), false);
    assert.equal(cardHidden(document, "maintenance"), false);
  });
});
