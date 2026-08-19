#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${VERSION:-1.5.1}
ARCH=${ARCH:-x86_64}
OUTPUT_DIR=${OUTPUT_DIR:-"$ROOT_DIR/dist"}
STAGING="$OUTPUT_DIR/omarag-$VERSION-linux-$ARCH"

mkdir -p "$OUTPUT_DIR"
rm -rf "$STAGING"
mkdir -p "$STAGING"

cargo build --manifest-path "$ROOT_DIR/Cargo.toml" --workspace --release --locked
install -m755 "$ROOT_DIR/target/release/omarag" "$STAGING/omarag-bin"
install -m755 "$ROOT_DIR/target/release/omarag-cli" "$STAGING/omarag-cli-bin"
install -m755 "$ROOT_DIR/scripts/omarag-update.sh" "$STAGING/omarag-update"
install -m644 "$ROOT_DIR/deploy/systemd/omarag.service.in" \
    "$STAGING/omarag.service"
install -m644 "$ROOT_DIR/deploy/systemd/omarag-update.service.in" \
    "$STAGING/omarag-update.service"
install -m644 "$ROOT_DIR/deploy/systemd/omarag-update.timer" \
    "$STAGING/omarag-update.timer"

tar -C "$STAGING" -czf \
    "$OUTPUT_DIR/omarag-$VERSION-linux-$ARCH.tar.gz" .
uv build --project "$ROOT_DIR/python" --wheel --out-dir "$OUTPUT_DIR"
VERSION=$VERSION ARCH=$ARCH "$ROOT_DIR/scripts/build-appimage.sh" \
    "$ROOT_DIR/target/release/omarag" "$OUTPUT_DIR"
install -m755 "$ROOT_DIR/scripts/install.sh" "$OUTPUT_DIR/install.sh"

(cd "$OUTPUT_DIR" && sha256sum \
    "omarag-$VERSION-linux-$ARCH.tar.gz" \
    "omarag_bridge-$VERSION-py3-none-any.whl" \
    "OmaRag-$VERSION-$ARCH.AppImage" \
    "OmaRag-$VERSION-$ARCH.AppImage.zsync" \
    install.sh > SHA256SUMS)
printf 'Release files are ready in %s\n' "$OUTPUT_DIR"
