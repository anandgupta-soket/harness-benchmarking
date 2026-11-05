const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { addTodo, toggleTodo, filterTodos } = require("../js/todo.js");

describe("addTodo", () => {
  it("appends a trimmed incomplete item", () => {
    const next = addTodo([], "  write tests  ");
    assert.equal(next.length, 1);
    assert.equal(next[0].title, "write tests");
    assert.equal(next[0].completed, false);
    assert.equal(next[0].id, "1");
  });

  it("ignores blank titles", () => {
    assert.deepEqual(addTodo([], "   "), []);
  });
});

describe("toggleTodo", () => {
  it("flips completed for the matching id", () => {
    const start = addTodo([], "one");
    const toggled = toggleTodo(start, start[0].id);
    assert.equal(toggled[0].completed, true);
    assert.equal(toggleTodo(toggled, start[0].id)[0].completed, false);
  });
});

describe("filterTodos", () => {
  it("returns all / active / completed slices", () => {
    let todos = addTodo([], "a");
    todos = addTodo(todos, "b");
    todos = toggleTodo(todos, todos[0].id);
    assert.equal(filterTodos(todos, "all").length, 2);
    assert.equal(filterTodos(todos, "active").length, 1);
    assert.equal(filterTodos(todos, "completed").length, 1);
    assert.equal(filterTodos(todos, "active")[0].title, "b");
  });
});
