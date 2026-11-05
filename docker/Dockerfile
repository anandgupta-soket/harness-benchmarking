# syntax=docker/dockerfile:1.7
# Build the one runner image used by controller, trial, and verifier pods.
FROM rust:1.88.0-bookworm AS loop-build
ARG LOOP_COMMIT=1dc7ef4a96b19e450d33629035341f0725b38f81
RUN apt-get update && apt-get install -y --no-install-recommends git pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN rustup component add clippy rustfmt --toolchain 1.88.0-x86_64-unknown-linux-gnu
RUN git init /src/loop \
    && git -C /src/loop remote add origin https://github.com/soketlabs/loop.git \
    && git -C /src/loop fetch --depth=1 origin "${LOOP_COMMIT}" \
    && git -C /src/loop checkout --detach FETCH_HEAD \
    && test "$(git -C /src/loop rev-parse HEAD)" = "${LOOP_COMMIT}"
WORKDIR /src/loop
RUN cargo build --locked --release -p loop-cli \
    && install -D target/release/loop /out/loop

FROM node:22.21.0-bookworm-slim
ARG LOOP_COMMIT=1dc7ef4a96b19e450d33629035341f0725b38f81
ARG PI_VERSION=0.73.1
ARG GOOSE_VERSION=1.57.0
ARG OPENCODE_VERSION=1.19.5
ARG HERMES_VERSION=0.22.7
ARG CLINE_VERSION=3.0.74
ARG KILO_VERSION=7.8.18
ARG OMP_VERSION=18.16.0
ARG DSH_VERSION=0.2.1rc2
ARG UV_VERSION=0.9.28
ARG KUBECTL_VERSION=1.30.14

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    BENCH_NODE_PATH=/opt/bench-tools/node_modules \
    DSH_PYTHON=/opt/dsh-venv/bin/python \
    RUSTUP_HOME=/usr/local/rustup \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH=/opt/bench/.venv/bin:/opt/bench-tools/node_modules/.bin:/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python-is-python3 curl git ca-certificates jq xz-utils bzip2 \
      build-essential pkg-config libssl-dev libsqlite3-0 libffi8 libstdc++6 \
      procps tini \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/kubectl \
    && kubectl version --client --output=yaml | grep -F "gitVersion: v${KUBECTL_VERSION}"

# uv is pinned because it resolves the locked runner environment and provisions
# the Python 3.14 runtime required by current Hermes releases.
RUN python3 -m pip install --break-system-packages --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/bench
COPY pyproject.toml uv.lock ./
COPY bench/ ./bench/
COPY scripts/ ./scripts/
COPY tasks/ ./tasks/
COPY fixtures/ ./fixtures/
COPY controls/ ./controls/
COPY verifiers/ ./verifiers/
COPY tests/ ./tests/
COPY --from=loop-build /out/loop /usr/local/bin/loop
COPY --from=loop-build /usr/local/cargo /usr/local/cargo
COPY --from=loop-build /usr/local/rustup /usr/local/rustup
COPY docker/image-manifest.json ./image-manifest.json

# Install controller/verifier dependencies from the committed lock, then pin
# Playwright's browser to the matching package version. The browser and fixture
# node_modules are trusted image assets and never come from agent workspaces.
RUN uv sync --frozen --no-dev --python /usr/bin/python3 \
    && npm ci --prefix /opt/bench/fixtures/dark-mode-site --ignore-scripts \
    && /opt/bench/.venv/bin/python -m playwright install --with-deps chromium \
    && install -d /opt/dsh-venv \
    && uv venv --python /usr/bin/python3 /opt/dsh-venv \
    && uv pip install --python /opt/dsh-venv/bin/python "deepseek-harness-sdk==${DSH_VERSION}"

# Install the four npm CLIs from the local lockfile generated alongside this
# Dockerfile. Harness commands are pinned in configs/glm53.yaml and mirrored by
# these explicit versions.
COPY docker/package.json docker/package-lock.json /opt/bench-tools/
RUN npm ci --prefix /opt/bench-tools \
    && npm ls --prefix /opt/bench-tools --depth=0 \
    && /opt/bench/.venv/bin/python -c 'from importlib.metadata import version; assert version("playwright") == "1.64.1"' \
    && ln -s /opt/bench-tools/node_modules/.bin/pi /usr/local/bin/pi \
    && ln -s /opt/bench-tools/node_modules/.bin/opencode /usr/local/bin/opencode \
    && ln -s /opt/bench-tools/node_modules/.bin/cline /usr/local/bin/cline \
    && ln -s /opt/bench-tools/node_modules/.bin/kilo /usr/local/bin/kilo

# Goose publishes a versioned installer in every release; set GOOSE_VERSION so
# its downloaded binary is exactly the version used by the experiment.
RUN curl -fsSL "https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/download_cli.sh" \
      -o /tmp/goose-install.sh \
    && CONFIGURE=false GOOSE_VERSION="v${GOOSE_VERSION}" GOOSE_BIN_DIR=/usr/local/bin bash /tmp/goose-install.sh \
    && rm -f /tmp/goose-install.sh \
    && goose --version | grep -F "${GOOSE_VERSION}"

# Hermes must be installed from its pinned source release (PyPI is stale and
# current source installs require Python 3.14); OMP ships a pinned Linux binary.
RUN git init /opt/hermes-src \
    && git -C /opt/hermes-src remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes-src fetch --depth=1 origin "v${HERMES_VERSION}" \
    && git -C /opt/hermes-src checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes-src describe --tags --exact-match)" = "v${HERMES_VERSION}" \
    && cd /opt/hermes-src \
    && uv sync --locked --python 3.14 --no-dev \
    && ln -s /opt/hermes-src/.venv/bin/hermes /usr/local/bin/hermes \
    && curl -fsSL "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/omp-linux-x64" \
       -o /usr/local/bin/omp \
    && echo '6498ed785bab00d2926e0387e0ec02362d6d45f1c3d0d1b57c1618b7f95c9264  /usr/local/bin/omp' | sha256sum -c - \
    && chmod 0755 /usr/local/bin/omp \
    && node -e 'for (const [p,v] of [["@mariozechner/pi-coding-agent", process.env.PI_VERSION], ["opencode-ai", process.env.OPENCODE_VERSION], ["cline", process.env.CLINE_VERSION], ["@kilocode/cli", process.env.KILO_VERSION]]) { const actual = require(`/opt/bench-tools/node_modules/${p}/package.json`).version; if (actual !== v) throw new Error(`${p}: ${actual} != ${v}`) }' \
    && loop --version | grep -F '0.6.0' \
    && pi --version | grep -F "${PI_VERSION}" \
    && opencode --version | grep -F "${OPENCODE_VERSION}" \
    && cline --version | grep -F "${CLINE_VERSION}" \
    && kilo --version | grep -F "${KILO_VERSION}" \
    && omp --version | grep -F "${OMP_VERSION}" \
    && hermes --version | grep -F "${HERMES_VERSION}"

# No agent UID is the container's default: the worker supervisor must start as
# root to drop privileges, keep artifacts root-only, and run independent checks.
# Agent subprocesses are switched to UID 10001 by bench.worker.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bench-agent \
    && chmod 0755 /opt/bench /opt/bench/bench /opt/bench/scripts /opt/bench/tasks \
    && chmod -R go-rwx /opt/bench/controls /opt/bench/verifiers /opt/bench/tests /opt/bench/fixtures \
    && chmod 0400 /opt/bench/bench/verification.py \
    && find /opt/bench -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /opt/bench -type f -name '*.pyc' -exec chmod 0400 {} + \
    && chmod -R a+rX /opt/pw-browsers /opt/python /opt/bench/fixtures/dark-mode-site/node_modules \
    && chmod 0711 /opt/bench/fixtures /opt/bench/fixtures/dark-mode-site \
    && chmod -R a-w /opt/bench /opt/bench-tools /opt/hermes-src /opt/dsh-venv /opt/pw-browsers /opt/python

WORKDIR /opt/bench
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bench.controller"]
