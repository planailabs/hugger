#!/usr/bin/env bash
# Build the hugger OCI image with nix and copy it to the GitLab registry.
# Run via: nix develop -c bash docker-push.sh
set -euxo pipefail

if ! command -v skopeo >/dev/null 2>&1; then
  echo "error: skopeo is not available; run via 'nix develop -c bash docker-push.sh'" >&2
  exit 127
fi

copy_image() {
  local archive="$1"
  local destination="$2"
  local xtrace_was_on=0
  case "$-" in
    *x*) xtrace_was_on=1; set +x ;;
  esac
  skopeo copy \
    "docker-archive:${archive}" \
    "${destination}" \
    --dest-creds "${CI_REGISTRY_USER}:${CI_REGISTRY_PASSWORD}"
  [ "$xtrace_was_on" -eq 1 ] && set -x || true
}

# Ensure skopeo trust policy exists (CI runners may lack it).
POLICY_DIR="/etc/containers"
mkdir -p "$POLICY_DIR" 2>/dev/null || POLICY_DIR="$HOME/.config/containers"
[ -w "$POLICY_DIR" ] || POLICY_DIR="$HOME/.config/containers"
mkdir -p "$POLICY_DIR"
[ -f "$POLICY_DIR/policy.json" ] || echo '{"default":[{"type":"insecureAcceptAnything"}]}' > "$POLICY_DIR/policy.json"

REGISTRY="${CI_REGISTRY:-git.plan.ai:5050}"
PROJECT="${CI_PROJECT_PATH:-plan-ai/hugger}"
TAG="${CI_COMMIT_SHORT_SHA:-latest}"

nix build ".#docker" -L
archive="$(readlink -f result)"

copy_image "$archive" "docker://${REGISTRY}/${PROJECT}/hugger:${TAG}"
copy_image "$archive" "docker://${REGISTRY}/${PROJECT}/hugger:latest"
