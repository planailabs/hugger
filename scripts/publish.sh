#!/usr/bin/env bash
# Build per-store extension packages and publish them to all three stores:
# Chrome Web Store, Firefox Add-ons (AMO), and Microsoft Edge Add-ons.
#
# Per-store manifests are generated so no store's validator warns about another
# store's background key:
#   chrome/edge -> background.service_worker  (browser_specific_settings stripped)
#   firefox     -> background.scripts
#
# Usage:
#   scripts/publish.sh [all|chrome|firefox|edge] [--build-only]
#
# Tools (provided by `nix develop .#publish`): jq, zip, curl, web-ext, node/npx.
#
# Credentials (env):
#   Chrome:  CHROME_EXTENSION_ID CHROME_CLIENT_ID CHROME_CLIENT_SECRET CHROME_REFRESH_TOKEN
#   Firefox: WEB_EXT_API_KEY WEB_EXT_API_SECRET
#   Edge:    EDGE_PRODUCT_ID EDGE_API_KEY EDGE_CLIENT_ID
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="extension"
OUT="dist/extension"

TARGET="all"
BUILD_ONLY=false
for arg in "$@"; do
  case "$arg" in
    all|chrome|firefox|edge) TARGET="$arg" ;;
    --build-only) BUILD_ONLY=true ;;
    *) echo "usage: $0 [all|chrome|firefox|edge] [--build-only]"; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found — run inside 'nix develop .#publish'"; exit 1; }; }
need jq; need zip

VERSION="$(jq -r .version "$SRC/manifest.json")"
echo ">> hugger extension v$VERSION  (target: $TARGET, build-only: $BUILD_ONLY)"

# Chromium-family manifest (Chrome + Edge) and Firefox manifest.
CHROMIUM_FILTER='del(.background.scripts) | del(.browser_specific_settings)'
FIREFOX_FILTER='del(.background.service_worker)'

build() {
  local store="$1" filter="$2"
  local dir="$OUT/$store"
  rm -rf "$dir" "$OUT/$store.zip"
  mkdir -p "$dir"
  ( cd "$SRC" && find . -type f ! -name manifest.json -exec install -D {} "../$dir/{}" \; )
  jq "$filter" "$SRC/manifest.json" > "$dir/manifest.json"
  ( cd "$dir" && zip -qr -FS "../$store.zip" . )
  echo ">> built $OUT/$store.zip"
}

publish_chrome() {
  build chrome "$CHROMIUM_FILTER"
  $BUILD_ONLY && return 0
  : "${CHROME_EXTENSION_ID:?set CHROME_EXTENSION_ID}"
  : "${CHROME_CLIENT_ID:?set CHROME_CLIENT_ID}"
  : "${CHROME_CLIENT_SECRET:?set CHROME_CLIENT_SECRET}"
  : "${CHROME_REFRESH_TOKEN:?set CHROME_REFRESH_TOKEN}"
  need npx
  echo ">> uploading + publishing to Chrome Web Store…"
  npx --yes -p chrome-webstore-upload-cli chrome-webstore-upload upload \
    --source "$OUT/chrome.zip" \
    --extension-id "$CHROME_EXTENSION_ID" \
    --client-id "$CHROME_CLIENT_ID" \
    --client-secret "$CHROME_CLIENT_SECRET" \
    --refresh-token "$CHROME_REFRESH_TOKEN" \
    --auto-publish
}

publish_firefox() {
  build firefox "$FIREFOX_FILTER"
  $BUILD_ONLY && return 0
  : "${WEB_EXT_API_KEY:?set WEB_EXT_API_KEY}"
  : "${WEB_EXT_API_SECRET:?set WEB_EXT_API_SECRET}"
  need web-ext
  echo ">> linting firefox build…"
  web-ext lint --source-dir "$OUT/firefox"
  echo ">> signing + submitting to AMO (listed channel)…"
  web-ext sign --source-dir "$OUT/firefox" --channel listed \
    --api-key "$WEB_EXT_API_KEY" --api-secret "$WEB_EXT_API_SECRET" \
    --artifacts-dir "$OUT"
}

# Edge Add-ons API v1.1 (https://learn.microsoft.com/microsoft-edge/extensions-chromium/publish/api/using-addons-api)
edge_api() { curl -sS -H "Authorization: ApiKey $EDGE_API_KEY" -H "X-ClientID: $EDGE_CLIENT_ID" "$@"; }

edge_wait() { # $1 = operation status URL
  local status
  for _ in $(seq 1 120); do
    status="$(edge_api "$1" | jq -r '.status // "Unknown"')"
    case "$status" in
      Succeeded) return 0 ;;
      Failed|Cancelled) echo "   edge operation $status"; edge_api "$1"; return 1 ;;
    esac
    sleep 5
  done
  echo "   edge operation timed out"; return 1
}

publish_edge() {
  build edge "$CHROMIUM_FILTER"
  $BUILD_ONLY && return 0
  : "${EDGE_PRODUCT_ID:?set EDGE_PRODUCT_ID}"
  : "${EDGE_API_KEY:?set EDGE_API_KEY}"
  : "${EDGE_CLIENT_ID:?set EDGE_CLIENT_ID}"
  need curl
  local base="https://api.addons.microsoftedge.microsoft.com/v1/products/$EDGE_PRODUCT_ID"
  echo ">> uploading package to Edge Add-ons…"
  local loc
  loc="$(edge_api -X POST "$base/submissions/draft/package" \
        -H "Content-Type: application/zip" --data-binary @"$OUT/edge.zip" \
        -D - -o /dev/null | awk 'tolower($1)=="location:"{print $2}' | tr -d '\r')"
  [ -n "$loc" ] || { echo "   no upload operation id returned"; return 1; }
  edge_wait "$base/submissions/draft/package/operations/$loc"
  echo ">> publishing Edge submission…"
  loc="$(edge_api -X POST "$base/submissions" -d '{"notes":"automated publish"}' \
        -H "Content-Type: application/json" -D - -o /dev/null \
        | awk 'tolower($1)=="location:"{print $2}' | tr -d '\r')"
  [ -n "$loc" ] || { echo "   no publish operation id returned"; return 1; }
  edge_wait "$base/submissions/operations/$loc"
}

case "$TARGET" in
  chrome)  publish_chrome ;;
  firefox) publish_firefox ;;
  edge)    publish_edge ;;
  all)     publish_chrome; publish_firefox; publish_edge ;;
esac

echo ">> done."
