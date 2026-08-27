#!/usr/bin/env bash
# Remove everything bootstrap-local.sh created. Nothing else.
#
# Written on the same day as the bootstrap on purpose. You cannot honestly say what to
# stop without listing what starts, and writing the teardown is what makes an unused
# service or a stray directory visible.

set -euo pipefail

PREFIX="${PREFIX:-/tmp/wdc}"

if [ ! -d "$PREFIX" ]; then
  echo "nothing at $PREFIX"
  exit 0
fi

du -sh "$PREFIX" 2>/dev/null || true
rm -rf "$PREFIX"
echo "removed $PREFIX"

# data/raw is not touched. It is the fetched corpus and re-fetching it costs about 40
# seconds against a live API whose contents move, so throwing it away is a real loss.
# scripts/pull_source.py rebuilds it and data/manifest.json says whether what came back
# is what the published numbers were measured on.
echo "data/raw left alone. delete it by hand if you mean to."
