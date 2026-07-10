#!/bin/sh
set -eu

# Unit + HTTP integration + live-Hub tests (live tests skip when offline).
nix develop .#test -c python tests/test_core.py
nix develop .#test -c python tests/test_api.py
nix develop .#test -c python tests/test_hub_live.py
nix flake check -L
