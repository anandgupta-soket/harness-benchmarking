# Scored run: how it is split, and what a reviewer should know

The scored benchmark is 9 harnesses x 13 tasks x 4 memory limits (256/512/1024/2048 MiB) x 5 repetitions
= 2,340 trials, GLM-5.3 only, 1 CPU per trial.

To finish sooner it runs as two experiments in two namespaces, both pinned to `soketlab-worker001`
and using the same runner image digest:

| Run | Config | Namespace | Repetitions it is used for |
|---|---|---|---|
| A | `configs/glm53.yaml` | `bench-constrained` | 1, 2, 3 |
| B | `configs/glm53-reps45.yaml` | `bench-constrained-b` | 4, 5 |

`configs/glm53-reps45.yaml` differs from `configs/glm53.yaml` only in `experiment.name`,
`experiment.repetitions: 2` and `kube.namespace`. Run B's own repetitions 1 and 2 are reported as
overall repetitions 4 and 5.

Run A was started with `repetitions: 5`. It is cancelled once repetitions 1-3 are complete
(1,404 settled slots); any `r4.*` slots in run A are partial and must be excluded from analysis.
Always pass `--config` and `--run-id` to `bench` commands, because `.bench/latest.json` points at the
newest run only.

## Qualification status (disclose this when reporting results)

* Run A: resources, controls, protocol and matrix all passed for its config hash.
* Run B: resources and controls passed. The protocol stage failed 11 times, always and only on
  `goose: no recorded model usage and executed tool call`; every other harness passed every time in that
  namespace. On the operator's explicit instruction, run B was started without a passing protocol or matrix
  stage. Its `protocol.json` and `matrix.json` are labelled waivers (`"waived": true`), not real passes.

## Known measurement gap: Goose token usage

Goose's usage is scraped from its `sessions.db` right after the run and is recorded for only about 30% of
trials. The data is still present in each trial's saved `harness_state/goose-root/data/sessions/sessions.db`
and can be recovered after the run. Until then, Goose token columns are incomplete and should not be compared
with other harnesses.

## Cluster notes

* A stale `ResourceQuota` named `bench-quota` (8 pods) was removed from `bench-constrained` with the
  operator's approval; it was capping the namespace below the intended `bench-budget` (12 pods).
* Other users' workloads may share `bench-constrained` and count against `bench-budget`. They affect pod
  availability, not trial resource limits.
* Wall-clock times can drift while the cluster or the model backend is busier; compare harnesses within the
  same group (task x memory limit), which runs concurrently.
