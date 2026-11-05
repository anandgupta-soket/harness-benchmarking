#!/usr/bin/env bash
# WEB-REF-01: core tests still pass and bindEvents was extracted.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
node --test tests/todo.test.js
# Require a real function declaration, not a comment mention.
grep -Eq '^[[:space:]]*function bindEvents[[:space:]]*\(' js/app.js
