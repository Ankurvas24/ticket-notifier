#!/usr/bin/env bash
# Cross-platform git push helper (macOS / Linux / WSL / Git Bash on Windows).
# Runs from the repo root regardless of where it was invoked from.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="${GIT:-git}"

# Clean up any stuck locks from a previous Ctrl-C'd push
for f in .git/index.lock .git/HEAD.lock .git/config.lock \
         .git/refs/heads/main.lock .git/objects/maintenance.lock; do
  [ -f "$f" ] && rm -f "$f"
done

MSG="${1:-chore: cross-system hardening + stealth + cart URL fallbacks}"

{
  "$GIT" add -A
  "$GIT" commit -m "$MSG" || echo "(nothing to commit)"
  "$GIT" pull --rebase origin main
  "$GIT" push origin main
  echo DONE
} 2>&1 | tee git_output.log
