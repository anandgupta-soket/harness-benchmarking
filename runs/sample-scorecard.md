# Internal Beta Scorecard

- **Generated:** 2026-08-20 08:51 UTC
- **Source:** `runs/sample-runs.csv`
- **Runs:** 12
- **Tasks:** 2
- **Date range:** 2026-08-20 → 2026-08-20

## Overall (by harness)

| Harness | Success rate | Median wall clock | p90 wall clock | Median turns | Median tokens | Median first token |
|---|---:|---:|---:|---:|---:|---:|
| Claude Code | 100.0% | 1.5m | 2.4m | 3 | 12.8k | 520ms |
| Cursor | 100.0% | 1.6m | 3.1m | 5 | 22.3k | 700ms |
| Loop | 75.0% | 2.1m | 3.0m | 4 | 15.6k | 920ms |
| Codex | 50.0% | 1.7m | 1.7m | 4 | 17.4k | 780ms |

_Efficiency metrics (wall clock, turns, tokens, first token) computed on **successful runs only**._

## Per-task success rate

| Task | Claude Code | Cursor | Loop | Codex |
|---|---:|---:|---:|---:|
| BUG-01 | 100.0% | 100.0% | 66.7% | 50.0% |
| FEAT-01 | 100.0% | 100.0% | 100.0% | — |

## Token efficiency (successful runs, median total tokens)

- **Claude Code:** median 12.8k, p90 22.5k
- **Cursor:** median 22.3k, p90 35.3k
- **Loop:** median 15.6k, p90 27.7k
- **Codex:** median 17.4k, p90 17.4k

## Top failures

- **Loop** — BUG-01 (r003): timeout near max turns
- **Codex** — BUG-01 (r009): wrong edit reverted manually

## Suggested headline order

1. Success rate
2. Median wall clock (successful)
3. Median turns (successful)
4. Median tokens (successful)
