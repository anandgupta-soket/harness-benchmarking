const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { addTodo, toggleTodo, remainingCount } = require("../js/todo.js");

describe("remainingCount", () => {
  it("is 0 for an empty list", () => {
    assert.equal(remainingCount([]), 0);
  });

  it("counts only incomplete todos", () => {
    let todos = addTodo([], "a");
    todos = addTodo(todos, "b");
    todos = addTodo(todos, "c");
    todos = toggleTodo(todos, todos[0].id);
    assert.equal(remainingCount(todos), 2);
  });

  it("is 0 when every todo is completed", () => {
    let todos = addTodo([], "a");
    todos = toggleTodo(todos, todos[0].id);
    assert.equal(remainingCount(todos), 0);
  });
});
