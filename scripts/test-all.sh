#!/usr/bin/env bash
# Run the whole hugger test suite: Python unit/integration + both NixOS VM tests
# (API download, and the Chromium/Firefox extension E2E).
#
# Intended to run inside `nix develop .#test` (provides python+selenium and both
# browsers) or anywhere with nix available.
set -euo pipefail
cd "$(dirname "$0")/.."

system="$(nix eval --impure --raw --expr 'builtins.currentSystem' 2>/dev/null || echo x86_64-linux)"

echo "== python unit + integration tests =="
python tests/test_core.py
python tests/test_api.py

echo "== live HuggingFace Hub tests (skips if offline) =="
python tests/test_hub_live.py

echo "== nixos VM test: API download over self-signed HTTPS =="
nix build ".#checks.${system}.vm" -L

echo "== nixos VM test: Chromium + Firefox extension E2E =="
nix build ".#checks.${system}.browser" -L

echo "All test suites passed."
