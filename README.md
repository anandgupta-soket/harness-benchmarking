# Loop benchmark harness (internal beta)

Minimal fixtures and scripts for cross-harness evaluation: tokens, latency, turns, and success rate.

## Layout

```

├── fixtures/          # Self-contained repos for reproducible tasks
├── tasks/             # Version-controlled task specs (YAML)
├── runs/              # CSV run logs + generated scorecards
└── scripts/           # Aggregation and validation helpers
```

## Quick start

### 1) Pick tasks

```bash
ls tasks/core-coding/
python3 scripts/validate_tasks.py
```

### 2) Run a benchmark (manual for now)

For each `(harness, task)` pair:

1. Check out the task’s repo commit (or use `fixtures/*`).
2. Send the exact prompt from the task YAML.
3. Use **hands-off** operator policy: approve tools only, no hints.
4. Record one CSV row when done (copy from `runs/template.csv`).

Log file: `runs/runs.csv` (create from template; gitignored).

### 3) Generate scorecard

```bash
python3 scripts/aggregate_scorecard.py \
  --input runs/runs.csv \
  --output runs/scorecard.md
```

Try the sample data:

```bash
python3 scripts/aggregate_scorecard.py \
  --input runs/sample-runs.csv \
  --output runs/sample-scorecard.md
```

## Task spec format

See [`tasks/README.md`](tasks/README.md). One YAML file per task under `tasks/<suite>/`.

## CSV schema

```csv
run_id,date,harness,model,task_id,repo_sha,success,input_tokens,output_tokens,total_tokens,t_first_token_ms,t_last_token_ms,wall_clock_ms,assistant_turns,tool_calls,retries,notes
```

- `success`: `true` / `false` / `1` / `0`
- Durations in milliseconds; token fields integers (empty allowed for unknown)

## Related docs

- [`docs/evaluation-benchmark-plan.md`](../docs/evaluation-benchmark-plan.md) — process and rollout
- [`docs/feature-comparison.md`](../docs/feature-comparison.md) — capability matrix
