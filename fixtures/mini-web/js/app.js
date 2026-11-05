/**
 * DOM wiring for mini-web.
 *
 * REF-01: these querySelector / addEventListener blocks are intentionally
 * duplicated. Extract them into a bindEvents helper without changing
 * behavior.
 */
(function () {
  const api = window.MiniTodo;
  if (!api) {
    return;
  }

  let todos = [];
  let filter = "all";

  function render() {
    const list = document.querySelector("#todo-list");
    const remainingEl = document.querySelector("#remaining");
    const visible = api.filterTodos(todos, filter);

    list.innerHTML = "";
    visible.forEach((todo) => {
      const li = document.createElement("li");
      li.dataset.id = todo.id;
      if (todo.completed) {
        li.classList.add("completed");
      }

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = todo.completed;

      const title = document.createElement("span");
      title.textContent = todo.title;

      li.appendChild(checkbox);
      li.appendChild(title);
      list.appendChild(li);
    });

    const n = api.remainingCount(todos);
    remainingEl.textContent = n === 1 ? "1 left" : n + " left";

    document.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.getAttribute("data-filter") === filter);
    });
  }

  document.querySelector("#add-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.querySelector("#new-todo");
    todos = api.addTodo(todos, input.value);
    input.value = "";
    render();
  });

  document.querySelector("#todo-list").addEventListener("click", (event) => {
    const row = event.target.closest("li[data-id]");
    if (!row) {
      return;
    }
    if (event.target.matches("input[type='checkbox']")) {
      todos = api.toggleTodo(todos, row.dataset.id);
      render();
    }
  });

  document.querySelector('[data-filter="all"]').addEventListener("click", () => {
    filter = "all";
    render();
  });

  document.querySelector('[data-filter="active"]').addEventListener("click", () => {
    filter = "active";
    render();
  });

  document.querySelector('[data-filter="completed"]').addEventListener("click", () => {
    filter = "completed";
    render();
  });

  document.querySelector("#clear-completed").addEventListener("click", () => {
    todos = api.clearCompleted(todos);
    render();
  });

  render();
})();
