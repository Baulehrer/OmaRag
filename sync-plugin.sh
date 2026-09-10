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
# Stage OUTSIDE the plugins directory. Staging next to the target made the
# shell briefly discover and load a phantom plugin from the temp folder.
stage="$(mktemp -d "${TMPDIR:-/tmp}/omarag-stage.XXXXXX")"
mkdir -p "$stage/backend" "$stage/ui" "$stage/common" "$stage/service"
cp "$src/manifest.json" "$src/OMA.qml" "$src/BarWidget.qml" "$stage/"
cp "$src/backend/"*.qml "$stage/backend/"
cp "$src/ui/"*.qml "$src/ui/"*.js "$stage/ui/"
cp "$src/common/"*.qml "$src/common/qmldir" "$stage/common/"
cp "$src/service/"*.qml "$stage/service/"

omarchy-plugin-validate "$stage" || { rm -rf "$stage"; exit 1; }
rm -rf "$dst"
mv "$stage" "$dst"
echo "validate: ok"
find "$dst" -type f | sed "s|$dst/||" | sort
