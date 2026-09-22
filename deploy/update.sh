#!/usr/bin/env bash
# Update Doorman to the latest commit and restart the service.
#
#     ./deploy/update.sh
#
# Pulls, installs any new dependencies, restarts, and waits to confirm it came
# back up -- reporting the journal instead if it did not. Safe to run on a
# machine with no service installed: it updates the checkout and stops there.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# Only tracked edits can block a fast-forward; stray untracked files cannot.
if [ -n "$(git status --porcelain -uno)" ]; then
  echo "Tracked files have local edits -- commit, stash or discard them first:"
  git status --short -uno
  exit 1
fi

OLD="$(git rev-parse HEAD)"
git pull --ff-only --quiet
NEW="$(git rev-parse HEAD)"

if [ "$OLD" = "$NEW" ]; then
  echo "Already up to date ($(git log -1 --format=%h\ %s))."
else
  echo
  git --no-pager log --oneline "$OLD..$NEW"
  echo
  # Only reinstall when the dependency list actually moved.
  if ! git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
    echo "requirements.txt changed -- installing:"
    ./.venv/bin/pip install -q -r requirements.txt
  fi
  # The unit itself is generated at install time, so a change needs a re-run.
  if ! git diff --quiet "$OLD" "$NEW" -- deploy/doorman.service; then
    echo "NOTE: deploy/doorman.service changed. Apply it with:"
    echo "  sudo ./deploy/install-service.sh"
  fi
fi

if ! systemctl list-unit-files doorman.service >/dev/null 2>&1; then
  echo "No doorman.service installed here, so nothing to restart."
  exit 0
fi

echo "Restarting doorman..."
sudo systemctl restart doorman.service

PORT="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\+\).*/\1/p' /etc/doorman/config.json 2>/dev/null | head -1)"
for _ in $(seq 1 10); do
  systemctl is-active --quiet doorman.service || break
  if [ -z "${PORT:-}" ] || ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    echo "Doorman is back up${PORT:+ on port $PORT} ($(git log -1 --format=%h\ %s))."
    exit 0
  fi
  sleep 1
done

echo
echo "Doorman did not come back cleanly. Recent log:"
journalctl -u doorman.service -n 30 --no-pager || true
exit 1
