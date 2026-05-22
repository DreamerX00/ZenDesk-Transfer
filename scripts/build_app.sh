#!/usr/bin/env bash
# Build the Zendesk app bundle and produce a private-app zip suitable
# for upload via `zcli apps:push`.
#
# Usage:  ./scripts/build_app.sh [--clean]
# Output: dist/zd-transfer-app-<version>.zip
#
# Why a zip: a private (non-Marketplace) Zendesk app is installed by
# uploading a .zip via Admin Center → Apps and integrations →
# Zendesk Support apps → Upload private app, OR via `zcli apps:push`.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="$ROOT/ui"
DIST_DIR="$ROOT/dist"

if [[ "${1:-}" == "--clean" ]]; then
  rm -rf "$UI_DIR/assets/app-"*.js \
         "$UI_DIR/assets/app-"*.js.map \
         "$UI_DIR/assets/chunk-"*.js \
         "$UI_DIR/assets/asset-"*
fi

echo "==> npm install"
( cd "$UI_DIR" && npm install --no-fund --no-audit --silent )

echo "==> typecheck"
( cd "$UI_DIR" && npx tsc --noEmit )

echo "==> vite build"
( cd "$UI_DIR" && npx vite build )

echo "==> packaging zip"
mkdir -p "$DIST_DIR"
VERSION="$(node -p "require('$UI_DIR/package.json').version")"
ZIP_NAME="zd-transfer-app-${VERSION}.zip"
rm -f "$DIST_DIR/$ZIP_NAME"

# Bundle layout matches what Zendesk expects: manifest.json + assets/
# + translations/ at the top of the zip.
( cd "$UI_DIR" && zip -rq "$DIST_DIR/$ZIP_NAME" \
    manifest.json \
    assets \
    translations \
    -x "assets/*.map" )

echo "==> $DIST_DIR/$ZIP_NAME"
du -h "$DIST_DIR/$ZIP_NAME"
