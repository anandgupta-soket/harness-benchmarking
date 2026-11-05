// Black-box test for client-side validation on the contact form (see
// tasks/.../feat-05-form-validation.yaml):
//
//   1. submitting with an empty name/message, or an invalid email, is
//      blocked (no "Thanks" confirmation in #form-status) and each
//      invalid field gets a non-empty error in id="<field>-error"
//   2. fixing the fields and submitting again clears those errors and
//      shows the existing success message
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SITE_DIR = path.join(__dirname, "..");
const PAGE = "contact.html";

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

function setValue(dom, el, value) {
  const proto = Object.getPrototypeOf(el);
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

function fillForm(dom, { name, email, message }) {
  const { document } = dom.window;
  setValue(dom, document.getElementById("name"), name);
  setValue(dom, document.getElementById("email"), email);
  setValue(dom, document.getElementById("message"), message);
}

function submit(dom) {
  const { document } = dom.window;
  document
    .getElementById("contact-form")
    .dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
}

function errorText(document, field) {
  const el = document.getElementById(`${field}-error`);
  assert.ok(el, `expected an element with id="${field}-error"`);
  return el.textContent.trim();
}

describe("contact form validation", () => {
  it("blocks submit and shows errors when every field is invalid", async () => {
    const dom = loadPage();
    await tick(dom);
    fillForm(dom, { name: "", email: "not-an-email", message: "" });
    submit(dom);
    await tick(dom);

    const { document } = dom.window;
    assert.doesNotMatch(document.getElementById("form-status").textContent, /thanks/i);
    assert.notEqual(errorText(document, "name"), "");
    assert.notEqual(errorText(document, "email"), "");
    assert.notEqual(errorText(document, "message"), "");
  });

  it("accepts a valid submission and clears errors", async () => {
    const dom = loadPage();
    await tick(dom);

    fillForm(dom, { name: "", email: "not-an-email", message: "" });
    submit(dom);
    await tick(dom);

    fillForm(dom, { name: "Dana Reyes", email: "dana@example.com", message: "Hello, I'd like a quote." });
    submit(dom);
    await tick(dom);

    const { document } = dom.window;
    assert.match(document.getElementById("form-status").textContent, /thanks/i);
    assert.equal(errorText(document, "name"), "");
    assert.equal(errorText(document, "email"), "");
    assert.equal(errorText(document, "message"), "");
  });

  it("rejects a malformed email even when other fields are valid", async () => {
    const dom = loadPage();
    await tick(dom);
    fillForm(dom, { name: "Dana Reyes", email: "dana@", message: "Hello there." });
    submit(dom);
    await tick(dom);

    const { document } = dom.window;
    assert.doesNotMatch(document.getElementById("form-status").textContent, /thanks/i);
    assert.notEqual(errorText(document, "email"), "");
  });
});
