// Trusted browser-DOM regression checks for the behavior extraction task.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const ROOT = process.env.VERIFIER_FIXTURE;
function page() {
  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const dom = new JSDOM(html, { url: "http://todo.test/", runScripts: "dangerously" });
  dom.window.eval(fs.readFileSync(path.join(ROOT, "js/todo.js"), "utf8"));
  dom.window.eval(fs.readFileSync(path.join(ROOT, "js/app.js"), "utf8"));
  return dom;
}
const tick = (dom) => new Promise((resolve) => dom.window.setTimeout(resolve, 5));

describe("WEB-REF-01 event wiring regression", () => {
  it("adds a todo and toggles it through delegated list wiring", async () => {
    const dom = page(); await tick(dom);
    const d = dom.window.document;
    d.getElementById("new-todo").value = "keep behavior";
    d.getElementById("add-form").dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
    assert.equal(d.querySelectorAll("#todo-list li").length, 1);
    const checkbox = d.querySelector("#todo-list input[type=checkbox]");
    checkbox.click();
    assert.equal(d.querySelector("#todo-list li").classList.contains("completed"), true);
  });

  it("preserves the planted remainingCount and clearCompleted helper behavior", async () => {
    const dom = page(); await tick(dom);
    const d = dom.window.document;
    d.getElementById("new-todo").value = "completed remains under baseline helper";
    d.getElementById("add-form").dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
    d.querySelector("#todo-list input[type=checkbox]").click();
    assert.equal(d.getElementById("remaining").textContent, "1 left");
    d.getElementById("clear-completed").click();
    assert.equal(d.querySelectorAll("#todo-list li").length, 1);
  });
});
