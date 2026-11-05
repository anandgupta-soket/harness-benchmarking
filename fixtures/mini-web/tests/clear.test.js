const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { addTodo, toggleTodo, clearCompleted } = require("../js/todo.js");

describe("clearCompleted", () => {
  it("drops completed todos and keeps active ones", () => {
    let todos = addTodo([], "keep");
    todos = addTodo(todos, "done");
    todos = toggleTodo(todos, todos[1].id);
    const next = clearCompleted(todos);
    assert.equal(next.length, 1);
    assert.equal(next[0].title, "keep");
    assert.equal(next[0].completed, false);
  });

  it("is a no-op when nothing is completed", () => {
    const todos = addTodo([], "only");
    const next = clearCompleted(todos);
    assert.equal(next.length, 1);
    assert.equal(next[0].title, "only");
  });

  it("returns an empty list when every todo is completed", () => {
    let todos = addTodo([], "a");
    todos = toggleTodo(todos, todos[0].id);
    assert.deepEqual(clearCompleted(todos), []);
  });
});
