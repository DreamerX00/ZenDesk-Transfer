#!/usr/bin/env bash
# Push the built private app to a Zendesk target subdomain using zcli.
#
# Usage:  ./scripts/install_private.sh <target-subdomain>
# Pre-req: zcli (Zendesk CLI) installed and `zcli login` already run.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <target-subdomain>" >&2
  exit 2
fi

TARGET="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v zcli >/dev/null 2>&1; then
  cat >&2 <<'EOF'
zcli is not installed. Install it with:
  npm install -g @zendesk/zcli

Then log in:
  zcli login -i
EOF
  exit 1
fi

# Make sure a build exists before pushing.
if [[ ! -f "$ROOT/ui/assets/iframe.html" ]] \
   || ! ls "$ROOT/ui/assets/app-"*.js >/dev/null 2>&1; then
  echo "==> no build artifacts in ui/assets — running build first"
  "$ROOT/scripts/build_app.sh"
fi

echo "==> pushing private app to $TARGET.zendesk.com"
( cd "$ROOT/ui" && zcli apps:push --subdomain "$TARGET" )

echo "==> Done. Visit https://$TARGET.zendesk.com/agent and look for the ZD Config Transfer icon in the nav bar."
