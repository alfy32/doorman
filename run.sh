#!/usr/bin/env bash
# Start Doorman.  Usage: ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f local/config.json ] && [ -z "${DOORMAN_CONFIG:-}" ]; then
  echo "No site config found. Create one:"
  echo "  mkdir -p local && cp config.example.json local/config.json"
  echo "Then add your Kindoo token and environment id to local/config.json."
  exit 1
fi
exec ./.venv/bin/python -m doorman
