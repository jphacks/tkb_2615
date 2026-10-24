#!/usr/bin/env bash
set -euo pipefail

if [[ "${SHELTERSIM_ALLOW_LIVE_API_TESTS:-}" != "1" ]]; then
  echo "Set SHELTERSIM_ALLOW_LIVE_API_TESTS=1 in the development environment to enable paid integration tests." >&2
  exit 2
fi
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ANTHROPIC_API_KEY is required for the live integration test." >&2
  exit 2
fi

python -m pytest -m live_api tests/integration/test_live_api.py
