#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
BINARY=${1:-"$ROOT_DIR/target/release/omarag"}
OUTPUT_DIR=${2:-"$ROOT_DIR/dist"}
VERSION=${VERSION:-1.5.0}
ARCH=${ARCH:-x86_64}
APPIMAGETOOL=${APPIMAGETOOL:-"$ROOT_DIR/dist/appimagetool-$ARCH.AppImage"}
APPDIR="$OUTPUT_DIR/OmaRag.AppDir"
OUTPUT="$OUTPUT_DIR/OmaRag-$VERSION-$ARCH.AppImage"

if [ ! -x "$BINARY" ]; then
    printf 'Missing release binary: %s\n' "$BINARY" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps"
install -m755 "$BINARY" "$APPDIR/usr/bin/omarag"
install -m644 "$ROOT_DIR/deploy/omarag.desktop" "$APPDIR/omarag.desktop"
install -m644 "$ROOT_DIR/deploy/omarag.desktop" "$APPDIR/usr/share/applications/omarag.desktop"
install -m644 "$ROOT_DIR/assets/omarag.svg" "$APPDIR/omarag.svg"
install -m644 "$ROOT_DIR/assets/omarag.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/omarag.svg"

# The single-quoted lines are the generated AppRun script, not expressions for
# this build process to expand.
# shellcheck disable=SC2016
printf '%s\n' '#!/bin/sh' \
    'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)' \
    'ENV_FILE=${XDG_CONFIG_HOME:-$HOME/.config}/omarag/omarag.env' \
    'if [ -r "$ENV_FILE" ]; then' \
    '  set -a' \
    '  . "$ENV_FILE"' \
    '  set +a' \
    'fi' \
    'exec "$HERE/usr/bin/omarag" "$@"' > "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"

if [ ! -x "$APPIMAGETOOL" ]; then
    curl --fail --location --retry 3 \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-$ARCH.AppImage" \
        --output "$APPIMAGETOOL"
    chmod 755 "$APPIMAGETOOL"
fi

UPDATE_INFO="gh-releases-zsync|Baulehrer|OmaRag|latest|OmaRag-*-$ARCH.AppImage.zsync"
ARCH=$ARCH VERSION=$VERSION APPIMAGE_EXTRACT_AND_RUN=1 \
    "$APPIMAGETOOL" --comp xz --updateinformation "$UPDATE_INFO" "$APPDIR" "$OUTPUT"
chmod 755 "$OUTPUT"
ZSYNC_NAME=$(basename "$OUTPUT").zsync
if [ -f "$ZSYNC_NAME" ] && [ "$ZSYNC_NAME" != "$OUTPUT.zsync" ]; then
    mv "$ZSYNC_NAME" "$OUTPUT.zsync"
fi
printf 'Built %s\n' "$OUTPUT"
