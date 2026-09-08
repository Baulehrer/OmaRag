#!/usr/bin/env bash
# Copy the plugin into the Omarchy plugin directory.
#
# NEVER symlink the repo into ~/.config/omarchy/plugins/. The shell watches
# that directory and follows symlinks; pointing it at a git repo froze the
# whole session once already (2026-09-08). omarchy-plugin-validate refuses
# symlinks for exactly this reason.
#
# Only the files the plugin actually needs are copied — no docs, no dotfiles.
set -euo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dst="$HOME/.config/omarchy/plugins/kaufmann.omarag"

# Build beside the target and move it into place in one step. Every write
# inside the plugin directory triggers a shell rescan; staging keeps that
# down to a single event instead of one per file.
stage="$(mktemp -d "${dst}.stage.XXXXXX")"
mkdir -p "$stage/backend"
cp "$src/manifest.json" "$src/OMA.qml" "$stage/"
cp "$src/backend/"*.qml "$stage/backend/"

omarchy-plugin-validate "$stage" || { rm -rf "$stage"; exit 1; }
rm -rf "$dst"
mv "$stage" "$dst"
echo "validate: ok"
find "$dst" -type f | sed "s|$dst/||" | sort
