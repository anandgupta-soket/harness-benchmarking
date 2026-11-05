# Runner image

The controller, trial supervisor, trusted verifier, and pinned harness CLIs run from one Linux/amd64 image. Its version manifest is installed at `/opt/bench/image-manifest.json`; `configs/glm53.yaml` must use the digest printed by the publish workflow before a scored run.

## Publish to GHCR

Run the manual **Build and publish benchmark runner image** workflow with a new tag, for example:

```sh
gh workflow run build-image.yml --ref <branch> -f image_tag=runner-v1
```

The workflow uses the repository's `GITHUB_TOKEN` with package write access and publishes `ghcr.io/<owner>/loop-bench-runner:<tag>`. Its final step prints the immutable `tag@sha256:...` reference. Copy that exact reference into `images.runner`; do not score against a mutable tag.

The image build compiles Loop from the pinned commit, installs the exact CLI releases listed in the experiment config, fetches Chromium and the locked fixture dependencies, and checks installed versions and binary checksums. API keys are not build arguments or image contents; they enter a trial only through the Kubernetes Secret environment variable.

Agents run as UID 10001. The root worker and verifier retain access to image source, fixtures, and trusted checks; the agent UID cannot read controls, verifier code, fixture originals, test sources, or Python bytecode. The Cline provider key file exists only in one trial home and is removed after the run.
