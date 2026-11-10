#!/bin/sh
set -eu

# Phase markers with elapsed time: when GitLab kills this job at its `timeout:`
# the trace otherwise ends mid-derivation with no hint which step was running,
# which makes a slow cold build look identical to a hang. Each marker carries
# the wall-clock time and the seconds spent so far in this job.
t0=$(date +%s)
phase() {
    echo "=== [$(date -u +%H:%M:%SZ)] +$(( $(date +%s) - t0 ))s: $*"
}

# Unit + HTTP integration + live-Hub tests (live tests skip when offline).
phase "test_core"
nix develop .#test -c python tests/test_core.py
phase "test_xet_ranges"
nix develop .#test -c python tests/test_xet_ranges.py
phase "test_api"
nix develop .#test -c python tests/test_api.py
phase "test_hub_live"
nix develop .#test -c python tests/test_hub_live.py
phase "flake_check"
nix flake check -L
phase "done"
