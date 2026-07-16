#!/usr/bin/env bash
# Initialize the example target repo as a real git repo (needed once, because
# SimpleLoop clones it with `git clone --local`). The repo files are tracked in
# the SimpleLoop repo as plain files, without their own .git, so they don't ship
# as a broken submodule.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/repo"
if [ -d .git ]; then
  echo "repo already a git repo; nothing to do."
  exit 0
fi
git init -q
git add -A
git -c user.email=tinyalgo@example.invalid -c user.name=tinyalgo commit -qm "tinyalgo baseline"
echo "repo initialized at $HERE/repo ($(git rev-parse --short HEAD))"
