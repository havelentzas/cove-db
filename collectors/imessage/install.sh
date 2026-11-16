#!/bin/bash
# Install (or update) the cove iMessage collector on this Mac. Run it from this folder:
#   ./install.sh https://<your cove host>        install / update, and ask for the ingest token
#   ./install.sh --uninstall                     stop it and remove it (your config and token stay)
# Nothing here needs sudo. Message text never leaves this Mac except to your cove.
set -euo pipefail

LABEL="com.cove.imessage-collector"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/cove-collector"
CONF_DIR="$HOME/.config/cove-collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="/usr/bin/python3"
DOMAIN="gui/$(id -u)"

if [ "$(uname)" != "Darwin" ]; then echo "This installs on macOS only." >&2; exit 1; fi

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$DEST/cove_imessage.py"
  echo "Removed. Your config ($CONF_DIR) and the Keychain token are still there:"
  echo "  rm -r \"$CONF_DIR\"; security delete-generic-password -s cove-collector -a ingest-token"
  exit 0
fi

URL="${1:-}"
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Needs Python 3.9+ at $PYTHON. Run: xcode-select --install" >&2; exit 1
fi

mkdir -p "$DEST" "$CONF_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$CONF_DIR"
install -m 755 "$HERE/cove_imessage.py" "$DEST/cove_imessage.py"

if [ ! -f "$CONF_DIR/config.toml" ]; then
  if [ -z "$URL" ]; then read -r -p "Your cove's address (https://…): " URL; fi
  case "$URL" in https://*) ;; *) echo "The address must start with https://" >&2; exit 1;; esac
  cat > "$CONF_DIR/config.toml" <<CONF
# cove iMessage collector. The ingest token is in the Keychain, not here.
url = "${URL%/}"
source_key = "imessage-mac"
label = "iMessage on my Mac"
rescan_days = 7
initial_days = 30
batch_size = 500
CONF
  chmod 600 "$CONF_DIR/config.toml"
  echo "Wrote $CONF_DIR/config.toml"
fi

if ! security find-generic-password -s cove-collector -a ingest-token >/dev/null 2>&1; then
  echo
  echo "Paste the ingest token from cove (Messages > Settings > Collectors). It starts with cove_ing_."
  echo "It goes into your login Keychain; you won't see it as you paste."
  security add-generic-password -U -s cove-collector -a ingest-token -w
fi

REAL_PY="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
sed -e "s#__PYTHON__#$PYTHON#" -e "s#__SCRIPT__#$DEST/cove_imessage.py#" -e "s#__HOME__#$HOME#" \
  "$HERE/$LABEL.plist" > "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

cat <<MSG

Installed. It runs every 10 minutes; its log is ~/Library/Logs/cove-collector.log

One more step: Full Disk Access (macOS protects ~/Library/Messages).
  1. System Settings > Privacy & Security > Full Disk Access
  2. Click +, press Cmd-Shift-G and add BOTH of these, then switch them on:
       $PYTHON
       $REAL_PY
  3. Check it:   $PYTHON "$DEST/cove_imessage.py" --check

Then send your whole history once (it shows progress):
     $PYTHON "$DEST/cove_imessage.py" --backfill
MSG
