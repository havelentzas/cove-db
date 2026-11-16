#!/bin/sh
# Install (or update) cove sync into an Obsidian vault on this computer.
# Usage: sh install.sh "/path/to/Your Vault"
set -eu
VAULT=${1:?usage: sh install.sh /path/to/vault}
[ -d "$VAULT/.obsidian" ] || { echo "No .obsidian folder in $VAULT: open it in Obsidian once first."; exit 1; }
DIR="$VAULT/.obsidian/plugins/cove-sync"
mkdir -p "$DIR"
HERE=$(cd "$(dirname "$0")" && pwd)
cp "$HERE/main.js" "$HERE/manifest.json" "$HERE/styles.css" "$DIR/"
echo "Installed cove sync into $DIR"
echo "In Obsidian: Settings → Community plugins → turn on 'cove sync', then open its settings."
