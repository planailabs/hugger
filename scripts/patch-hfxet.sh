#!/usr/bin/env bash
# Install the patched hf_xet wheel (transfer counters — see nix/hf-xet-patched.nix)
# into the dev .venv. Re-run after any `uv sync` / `uv run`, which revert to the
# stock wheel from uv.lock; `nix run` does this automatically.
set -euo pipefail
cd "$(dirname "$0")/.."

wheel_dir="$(nix build .#hf-xet-wheel --no-link --print-out-paths)"
uv pip install --force-reinstall --no-deps "$wheel_dir"/*.whl
python -c 'import hf_xet; assert hasattr(hf_xet.ItemProgressReport, "transfer_bytes_completed")' \
  && echo "patched hf_xet installed"
