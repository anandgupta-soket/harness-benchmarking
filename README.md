# Loop constrained benchmark runner

This repository packages the existing Loop, Pi, Goose, OpenCode, Hermes, Cline, Kilo, OMP, and DeepSeek harness CLIs with task fixtures, Kubernetes isolation, orchestration, independent verification, and resource/token/latency collection. It does not introduce a new coding agent. Tasks and prompts live in `tasks/`; clean starter repositories live in `fixtures/`; adapter and verification code live in `bench/` and `verifiers/`.

The scored matrix is 9 harnesses × 13 tasks × 4 memory limits (256, 512, 1024, 2048 MiB) × 5 repetitions = **2,340 trials**. Every trial receives 1 CPU. A task/profile/repetition cell contains one trial per harness, all nine run concurrently (`max_concurrent_trials: 9`), and the cell is a barrier: every harness finishes before the next cell starts. Exactly one verifier pod runs at a time. The 2048 MiB condition is a reference candidate; it has not been proven unconstrained. Per-task timeout, CPU time, memory peak/OOM, token usage, latency, turns, tool calls, and verifier outcomes are retained. Token and turn ceilings are disclosed but not enforced.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- Docker Buildx for building/publishing the runner image and resolving its immutable digest.
- `kubectl` configured for the target cluster, or SSH access to the configured Kubernetes control host. For SSH mode, configure host-key verification in your normal SSH config; this runner does not bypass it.
- A Kubernetes namespace where you may create the benchmark quota, service account, role, PVC, reader, controller, trial, verifier, and qualification pods/jobs. The default config pins pods to `soketlab-worker001` because its `local-path` volume is node-local.
- A model endpoint and an API-key Kubernetes Secret in that namespace. Do not put the key in YAML, CLI arguments, or shell history.

Install dependencies and inspect the unresolved template:

```bash
uv sync --locked
uv run bench plan --config configs/glm53.yaml
```

`plan` is offline; it does not test the model endpoint or make model calls. The earlier public HTTPS route returned HTTP 502; a later single request to the configured internal route returned HTTP 200 from GLM-5.3. The deployment still requires the documented unscored qualification before scored runs.

## Configure and build the runner

Copy `configs/glm53.yaml` to a private operator config file. Set the endpoint/model/provider, worker node, Loop commit, and exact harness versions. Keep `model.api_key_secret` as a Secret name/key reference. Set `images.runner` to a published image tag temporarily while building; scored runs require a digest-pinned reference.

Build and publish from the repository root:

```bash
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile -t ghcr.io/OWNER/loop-bench-runner:TAG \
  --push .
uv run bench image resolve --image ghcr.io/OWNER/loop-bench-runner:TAG
```

Copy the returned `ghcr.io/OWNER/loop-bench-runner@sha256:…` into `images.runner`. The build embeds `/opt/bench/image-manifest.json`, including exact harness versions and the Loop source commit. Setup and run commands compare that manifest to the config and fail on drift. The registry package must be public for anonymous cluster pulls, or the namespace must be configured with an image-pull Secret. Do not use a mutable tag for scored runs.

The Docker build downloads pinned upstream harnesses and tools; it does not run a benchmark or call the model. Review `docker/image-manifest.json` and `docker/Dockerfile` before publishing an image.

## Prepare Kubernetes

Create the namespace first if it does not exist. Create the Secret interactively or through your secret manager. For example, when the key is available in the local environment as `BENCH_API_KEY`:

```bash
uv run bench setup --config /path/to/operator.yaml --api-key-env BENCH_API_KEY
```

Use `--replace-api-key` only when you intend to replace the configured Secret key. Secret values are sent directly to Kubernetes, are never read back to the local CLI, and are exposed only to trial pods. Setup applies the quota/RBAC/PVC, starts the small operator helper to bind the node-local volume, and checks image provenance. This helper has read/write access to the results PVC because qualification evidence and explicit replacement metadata must be persisted; it has no Kubernetes service-account token or model API key. Setup launches no model requests or benchmark trials. `--network-policy` optionally applies egress policy; enforcement depends on the cluster CNI.

For a pre-existing Secret, omit `--api-key-env`; setup checks only that the configured key name exists. Use `--local-kubectl` to use the current kubeconfig instead of SSH mode.

## Qualify before scored runs

Qualification evidence is written on the PVC and bound to the full configuration SHA-256. Changing any config field invalidates the evidence and requires qualification again.

```bash
uv run bench qualify --config /path/to/operator.yaml --stage all
```

This runs four gates in sequence: cgroup/resource probes at every configured profile; 13 positive and 13 negative verifier controls; a one-task-per-harness unscored protocol smoke; and the small unscored harness/task/profile matrix. Each gate stops the sequence on failure. The default protocol task is `BUG-01` at 2048 MiB, and the matrix covers every scored harness (enforced by config validation) on `BUG-01`, `FEAT-02` and `WEB-BUG-01` at 512 and 2048 MiB. Protocol qualification checks recorded usage, at least one executed tool call, and valid cgroup data; it is an integration smoke, not proof of backend model identity or complete protocol equivalence. These qualification runs do make model calls for protocol/matrix stages. Use a test endpoint and budget accordingly. To rerun one gate, pass `--stage resources`, `controls`, `protocol`, or `matrix`.

The scored command refuses to start unless all four passing records match the exact frozen config and its runner image manifest. No model/workload run is started by this documentation or by `plan`.

## Splitting a scored run across namespaces

A scored run can be split by repetitions to finish sooner. `configs/glm53.yaml` (namespace `bench-constrained`) covers repetitions 1-3 and `configs/glm53-reps45.yaml` (namespace `bench-constrained-b`, `repetitions: 2`) covers 4-5; the second run's repetitions 1-2 are reported as 4-5. Each config needs its own setup, API-key Secret and qualification, because the evidence is bound to the config hash. Always pass `--config` and `--run-id` explicitly, since `.bench/latest.json` only points at the newest run. See [docs/scored-run-split.md](docs/scored-run-split.md) for the merge rules, the qualification waiver on the second run, and the known Goose token-usage gap.

## Run and operate

```bash
uv run bench run --config /path/to/operator.yaml
uv run bench status --config /path/to/operator.yaml --watch
uv run bench logs --config /path/to/operator.yaml --trial ATTEMPT_UUID --follow
uv run bench pause --config /path/to/operator.yaml
uv run bench resume --config /path/to/operator.yaml
uv run bench cancel --config /path/to/operator.yaml
uv run bench replace --config /path/to/operator.yaml --slot SLOT_ID --reason 'worker disruption'
```

The controller and trials run in Kubernetes and continue if the local CLI disconnects. Pause stops dispatch and drains active work. Cancel marks live trials cancelled and leaves the controller available to finish cleanup. Resume is allowed only for a nonterminal paused run. A completed or cancelled run cannot be resumed; start a new run instead. Replacement requires a paused, drained run, a terminal unsuccessful attempt, and a reason; passed attempts cannot be replaced. Inspect `status` before replacement.

`bench task TASK_ID --harness HARNESS --memory 512Mi` launches one unscored task trial. It still requires a configured, digest-pinned runner and image provenance. `bench plan --stage protocol|matrix` shows the planned unscored scope without touching Kubernetes.

## Export and report

```bash
uv run bench export --config /path/to/operator.yaml --output exports
uv run bench report --input exports/RUN_ID.tar.gz --output reports/RUN_ID
```

Export requires a completed run or a paused, drained run. It uses a consistent SQLite backup, writes a SHA-256 sidecar, and includes run artifacts and matching qualification records. Report verifies the sidecar when present, rejects unsafe archive paths, and writes JSON, CSV, HTML, and (when installed) Parquet outputs. Qualification results are labeled separately from scored results and are not pooled into scores.

## Local checks

```bash
uv run pytest
uv run python controls/run_controls.py
```

These checks exercise local orchestration, fixtures, image manifest structure, and verifier controls. For AGENT-01, the verifier checks the final code and test result; the captured terminal logs support workflow review, but the before/after test sequence is not automatically graded. Local checks do not establish cluster readiness, public image visibility, API endpoint health, or model behavior.
