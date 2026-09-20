#!/usr/bin/env bash
# Install Doorman as a systemd service. Run on the server, from the clone:
#
#     sudo ./deploy/install-service.sh
#
# Config goes to /etc/doorman/config.json and mutable state (database, session
# key) to /var/lib/doorman, so neither is lost if the checkout is re-cloned.
# Re-run any time to pick up changes to deploy/doorman.service.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-root}"
ETC_DIR=/etc/doorman
STATE_DIR=/var/lib/doorman
UNIT=/etc/systemd/system/doorman.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Needs root to write $UNIT and $ETC_DIR. Re-run:"
  echo "  sudo $0"
  exit 1
fi

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "No virtualenv at $DIR/.venv. As $RUN_AS, first run:"
  echo "  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
  exit 1
fi

GROUP="$(id -gn "$RUN_AS")"

# --- config -----------------------------------------------------------------
install -d -m 0755 "$ETC_DIR"
if [ ! -f "$ETC_DIR/config.json" ]; then
  if [ -f "$DIR/local/config.json" ]; then
    install -o "$RUN_AS" -g "$GROUP" -m 0600 "$DIR/local/config.json" "$ETC_DIR/config.json"
    echo "Copied local/config.json -> $ETC_DIR/config.json (0600, $RUN_AS)"
  else
    install -o "$RUN_AS" -g "$GROUP" -m 0600 "$DIR/config.example.json" "$ETC_DIR/config.json"
    echo "No local/config.json found, so $ETC_DIR/config.json is the EXAMPLE."
    echo "Fill it in before trusting the service -- it holds your Kindoo"
    echo "environment id, unit names and sign-up allow-list."
  fi
else
  echo "Kept existing $ETC_DIR/config.json"
fi

# --- state (database, session key) -----------------------------------------
# systemd creates STATE_DIR itself, but do it here too so an existing database
# can be seeded before the first start.
install -d -o "$RUN_AS" -g "$GROUP" -m 0750 "$STATE_DIR"
for f in doorman.db session.key; do
  if [ -f "$DIR/local/$f" ] && [ ! -e "$STATE_DIR/$f" ]; then
    cp -p "$DIR/local/$f" "$STATE_DIR/$f"
    chown "$RUN_AS:$GROUP" "$STATE_DIR/$f"
    echo "Copied local/$f -> $STATE_DIR/$f (original left in place)"
  fi
done

# --- unit -------------------------------------------------------------------
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_AS|g" -e "s|__GROUP__|$GROUP|g" \
    "$DIR/deploy/doorman.service" > "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable --now doorman.service
echo "Installed $UNIT (runs as $RUN_AS)"

# --- verify -----------------------------------------------------------------
# enable --now returns before uvicorn has bound its port; give it a moment and
# report what actually happened rather than assuming.
for _ in $(seq 1 10); do
  systemctl is-active --quiet doorman.service || break
  PORT="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\+\).*/\1/p' "$ETC_DIR/config.json" | head -1)"
  if [ -n "${PORT:-}" ] && ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    echo
    echo "Doorman is up and listening on port $PORT."
    echo "It binds 127.0.0.1, so reach it from another machine with:"
    echo "  ssh -L $PORT:127.0.0.1:$PORT $RUN_AS@$(hostname)"
    exit 0
  fi
  sleep 1
done

echo
if systemctl is-active --quiet doorman.service; then
  echo "Service is running but nothing is listening yet. Recent log:"
else
  echo "Service is NOT running. Recent log:"
fi
journalctl -u doorman.service -n 30 --no-pager || true
exit 1
