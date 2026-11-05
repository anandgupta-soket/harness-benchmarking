# Task fixture format

Each benchmark task is a YAML file. Filename should match `id` (kebab-case).

## Schema

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Stable task id, e.g. `BUG-01` |
| `suite` | yes | `core-coding`, `agentic`, or `context` |
| `category` | yes | `bugfix`, `feature`, `refactor`, `tooling`, `understanding` |
| `title` | yes | Short human label |
| `repo` | yes | Where to run (see below) |
| `prompt` | yes | Exact user message (multiline string) |
| `success` | yes | Automated or manual verification |
| `limits` | yes | Stop conditions |
| `operator_policy` | yes | Usually `hands_off` |
| `tags` | no | Freeform labels |

### `repo`

```yaml
repo:
  type: fixture          # fixture | loop | external
  path: fixtures/mini-calc
  commit: HEAD           # pin SHA for external/loop; fixtures use HEAD
```

- **fixture** — path relative to Loop repo root; self-contained mini project.
- **loop** — this repository; set `commit` to a pinned SHA for rigor.
- **external** — clone URL + commit (document in run notes).

### `success`

**Automated (preferred):**

```yaml
success:
  type: command
  cwd: fixtures/mini-calc
  command: cargo test
  expect_exit: 0
```

**File check:**

```yaml
success:
  type: file_contains
  path: fixtures/mini-calc/src/lib.rs
  pattern: "a + b"
```

**Manual (understanding tasks):**

```yaml
success:
  type: manual
  checklist:
    - Names AgentHarness as orchestrator
    - Mentions session store backends
  reviewer_notes: "Score 1-5; success if >=4"
```

### `limits`

```yaml
limits:
  timeout_minutes: 20
  max_turns: 20
  max_tokens: 200000
```

## Adding a task

1. Copy `tasks/_template.yaml`.
2. Add fixture under `fixtures/` if needed.
3. Run `python3 scripts/validate_tasks.py`.
4. Register in `tasks/suite.yaml`.
