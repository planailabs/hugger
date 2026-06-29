#!/usr/bin/env bash
# Build the self-hosted Tailwind stylesheet from hugger/styles/input.css, scanning
# the FastHTML markup for class names. Re-run after changing markup or styles.
# tailwindcss is provided by `nix develop` (devShells.default / .#test).
set -euo pipefail
cd "$(dirname "$0")/.."
tailwindcss -c tailwind.config.js -i hugger/styles/input.css -o hugger/static/tailwind.css --minify
echo "built hugger/static/tailwind.css ($(wc -c <hugger/static/tailwind.css) bytes)"
