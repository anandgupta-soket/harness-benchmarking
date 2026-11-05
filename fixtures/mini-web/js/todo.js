/**
 * Pure todo helpers for the mini-web benchmark fixture.
 * Loaded as a classic script in the browser and via CommonJS in Node tests.
 */

function addTodo(todos, title) {
  const trimmed = String(title).trim();
  if (!trimmed) {
    return todos.slice();
  }
  const maxId = todos.reduce((max, todo) => {
    const id = Number(todo.id);
    return Number.isFinite(id) ? Math.max(max, id) : max;
  }, 0);
  return todos.concat({
    id: String(maxId + 1),
    title: trimmed,
    completed: false,
  });
}

function toggleTodo(todos, id) {
  return todos.map((todo) =>
    todo.id === String(id) ? { ...todo, completed: !todo.completed } : todo
  );
}

function remainingCount(todos) {
  // BUG: counts all todos instead of incomplete ones — agents should
  // return todos.filter((todo) => !todo.completed).length
  return todos.length;
}

function filterTodos(todos, filter) {
  if (filter === "active") {
    return todos.filter((todo) => !todo.completed);
  }
  if (filter === "completed") {
    return todos.filter((todo) => todo.completed);
  }
  return todos.slice();
}

function clearCompleted(todos) {
  // FEAT: stub — agents should return todos.filter((todo) => !todo.completed)
  return todos.slice();
}

const MiniTodo = {
  addTodo,
  toggleTodo,
  remainingCount,
  filterTodos,
  clearCompleted,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = MiniTodo;
}
globalThis.MiniTodo = MiniTodo;
