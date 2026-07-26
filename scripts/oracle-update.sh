#!/bin/sh
set -eu

REPOSITORY=${ORACLE_REPOSITORY:-Baulehrer/oracle-of-daedalus}
API_URL="https://api.github.com/repos/$REPOSITORY/releases/latest"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION_FILE="$PREFIX/share/oracle-of-daedalus/VERSION"
CURRENT_VERSION=${ORACLE_VERSION:-$(cat "$VERSION_FILE" 2>/dev/null || printf '0.0.0')}
ASSUME_YES=0

if [ "${1:-}" = "--yes" ]; then
    ASSUME_YES=1
fi

LATEST_TAG=$(curl --fail --silent --show-error --location "$API_URL" \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | head -n 1)
LATEST_VERSION=${LATEST_TAG#v}

if [ -z "$LATEST_VERSION" ]; then
    printf 'Could not determine the latest release.\n' >&2
    exit 1
fi
if [ "$LATEST_VERSION" = "$CURRENT_VERSION" ]; then
    printf 'Oracle of Dædalus %s is already current.\n' "$CURRENT_VERSION"
    exit 0
fi

printf 'Update available: %s -> %s\n' "$CURRENT_VERSION" "$LATEST_VERSION"
if [ "$ASSUME_YES" -ne 1 ]; then
    printf 'Install now? [y/N] '
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) exit 0 ;;
    esac
fi

installer=$(mktemp "${TMPDIR:-/tmp}/oracle-install.XXXXXX")
trap 'rm -f "$installer"' EXIT HUP INT TERM
curl --fail --location --retry 3 \
    "https://github.com/$REPOSITORY/releases/download/$LATEST_TAG/install.sh" \
    --output "$installer"
chmod 700 "$installer"
exec "$installer" --version "$LATEST_VERSION" --prefix "$PREFIX" --upgrade
