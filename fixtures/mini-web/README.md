# mini-web

Vanilla HTML/JS todo page used as a Loop benchmark fixture (bugfix / feature / refactor).

Open [`index.html`](index.html) in a browser. Automated grading does **not** use the browser; it runs `node --test`.

## Intentional starting state

| Area | File | Starting bug / gap |
|------|------|--------------------|
| Bug | `js/todo.js` `remainingCount` | Counts **all** todos, not incomplete ones |
| Feature | `js/todo.js` `clearCompleted` | Stub: returns a shallow copy, does not drop completed items |
| Refactor | `js/app.js` | Duplicated `querySelector` / `addEventListener` blocks; no `bindEvents()` helper |

Core tests in `tests/todo.test.js` pass on this tree. `tests/remaining.test.js` and `tests/clear.test.js` fail until their tasks are solved.

## Commands

```bash
# Core behavior (passes on the pristine fixture)
node --test tests/todo.test.js

# WEB-BUG-01 (fails until remainingCount is fixed)
node --test tests/remaining.test.js

# WEB-FEAT-01 (fails until clearCompleted is implemented)
node --test tests/clear.test.js

# WEB-REF-01
bash scripts/check_refactor.sh
```

Requires Node.js 18+ (`node --test`). No npm install.

## Reset protocol

BUG-01 and FEAT-01 share this tree and **will collide** if you do not reset between runs.

From the Loop repo root:

```bash
git checkout -- fixtures/mini-web
```

Or recopy the fixture from version control before every `(harness, task)` pair.
