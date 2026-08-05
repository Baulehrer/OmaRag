#!/bin/sh
set -eu

REPOSITORY=${OMARAG_REPOSITORY:-${ORACLE_REPOSITORY:-Baulehrer/OmaRag}}
VERSION=1.0.0
PREFIX=${ORACLE_PREFIX:-"$HOME/.local"}
INSTALL_SERVICE=1
INSTALL_UPDATER=1
DRY_RUN=0

usage() {
    printf '%s\n' \
        'Usage: install.sh [--version VERSION] [--prefix PATH] [--no-service]' \
        '                  [--no-auto-update] [--upgrade] [--dry-run]'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version) VERSION=$2; shift 2 ;;
        --prefix) PREFIX=$2; shift 2 ;;
        --no-service) INSTALL_SERVICE=0; shift ;;
        --no-auto-update) INSTALL_UPDATER=0; shift ;;
        --upgrade) shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$(uname -m)" in
    x86_64|amd64) ARCH=x86_64 ;;
    *) printf 'OmaRag 1.0 supports x86_64 Linux only.\n' >&2; exit 1 ;;
esac

TAG="v$VERSION"
BASE_URL=${ORACLE_BASE_URL:-"https://github.com/$REPOSITORY/releases/download/$TAG"}
ARCHIVE="omarag-$VERSION-linux-$ARCH.tar.gz"
WHEEL="omarag_bridge-$VERSION-py3-none-any.whl"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/oracle-install.XXXXXX")
CANDIDATE_VENV=
cleanup() {
    rm -rf "$WORK_DIR"
    if [ -n "$CANDIDATE_VENV" ] && [ -d "$CANDIDATE_VENV" ]; then
        rm -rf "$CANDIDATE_VENV"
    fi
}
trap cleanup EXIT HUP INT TERM

printf 'OmaRag %s · Oracle of Metis & Aletheia -> %s\n' "$VERSION" "$PREFIX"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'Would download %s and %s, verify SHA-256, install the daemon, client and updater.\n' \
        "$ARCHIVE" "$WHEEL"
    exit 0
fi

for command in curl sha256sum tar; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    }
done

curl --fail --location --retry 3 "$BASE_URL/$ARCHIVE" --output "$WORK_DIR/$ARCHIVE"
curl --fail --location --retry 3 "$BASE_URL/$WHEEL" --output "$WORK_DIR/$WHEEL"
curl --fail --location --retry 3 "$BASE_URL/SHA256SUMS" --output "$WORK_DIR/SHA256SUMS"
(cd "$WORK_DIR" && sha256sum --check --ignore-missing SHA256SUMS)
tar -xzf "$WORK_DIR/$ARCHIVE" -C "$WORK_DIR"

mkdir -p "$PREFIX/bin" "$PREFIX/share/oracle-of-daedalus"
install -m755 "$WORK_DIR/oracle-bin" "$PREFIX/bin/oracle-bin"
install -m755 "$WORK_DIR/oracle-cli-bin" "$PREFIX/bin/oracle-cli-bin"
printf '%s\n' "$VERSION" > "$PREFIX/share/oracle-of-daedalus/VERSION"

if ! command -v uv >/dev/null 2>&1; then
    curl --fail --location --retry 3 https://astral.sh/uv/install.sh --output "$WORK_DIR/install-uv.sh"
    sh "$WORK_DIR/install-uv.sh"
    PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || {
    printf 'uv installation failed.\n' >&2
    exit 1
}

VENV="$PREFIX/share/oracle-of-daedalus/venv"
CANDIDATE_VENV="$PREFIX/share/oracle-of-daedalus/.venv-candidate-$$"
PREVIOUS_VENV="$PREFIX/share/oracle-of-daedalus/venv-previous"
uv venv --python 3.12 "$CANDIDATE_VENV"
uv pip install --python "$CANDIDATE_VENV/bin/python" --torch-backend=cpu \
    "$WORK_DIR/$WHEEL" 'haiku-rag-slim[docling,cross-encoder]>=0.72' 'pypdfium2>=5,<6'
printf 'Checking public Haiku compatibility...\n'
"$CANDIDATE_VENV/bin/python" -m omarag_bridge.compat_probe

# The running environment stays untouched until the newest dependency set has
# passed Oracle's public-API gate. Keep the last known-good runtime for repair.
if [ -d "$PREVIOUS_VENV" ]; then
    mv "$PREVIOUS_VENV" "$WORK_DIR/older-venv"
fi
if [ -d "$VENV" ]; then
    mv "$VENV" "$PREVIOUS_VENV"
fi
if ! mv "$CANDIDATE_VENV" "$VENV"; then
    if [ -d "$PREVIOUS_VENV" ]; then
        mv "$PREVIOUS_VENV" "$VENV"
    fi
    printf 'Could not activate the compatible runtime; previous version restored.\n' >&2
    exit 1
fi
CANDIDATE_VENV=

CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
CONFIG_DIR="$CONFIG_HOME/oracle-of-daedalus"
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
DATA_DIR="$DATA_HOME/oracle-of-daedalus"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
ENV_FILE="$CONFIG_DIR/oracle.env"
if [ ! -s "$ENV_FILE" ]; then
    TOKEN=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')
    umask 077
    {
        printf 'OMARAG_BEARER_TOKEN=%s\n' "$TOKEN"
        printf 'OMARAG_TOKEN=%s\n' "$TOKEN"
        printf 'OMARAG_DATA_DIR=%s\n' "$DATA_DIR"
    } > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

write_wrapper() {
    target=$1
    binary=$2
    version=$3
    temporary="$target.tmp"
    {
        printf '%s\n' '#!/bin/sh'
        printf "ENV_FILE='%s'\n" "$(printf %s "$ENV_FILE" | sed "s/'/'\\\\''/g")"
        # These expressions belong to the generated launcher.
        # shellcheck disable=SC2016
        printf '%s\n' 'if [ -r "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi'
        printf 'export ORACLE_VERSION=%s\n' "$version"
        # shellcheck disable=SC2016
        printf '%s\n' \
            'export MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}' \
            'export MALLOC_TRIM_THRESHOLD_=${MALLOC_TRIM_THRESHOLD_:-131072}' \
            'export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}' \
            'export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}' \
            'export MKL_NUM_THREADS=${MKL_NUM_THREADS:-4}' \
            'export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-4}' \
            'export NUMEXPR_NUM_THREADS=${NUMEXPR_NUM_THREADS:-4}' \
            'export RAYON_NUM_THREADS=${RAYON_NUM_THREADS:-4}'
        printf "exec '%s' \"\$@\"\n" "$(printf %s "$binary" | sed "s/'/'\\\\''/g")"
    } > "$temporary"
    chmod 755 "$temporary"
    mv "$temporary" "$target"
}
write_wrapper "$PREFIX/bin/oracle" "$PREFIX/bin/oracle-bin" "$VERSION"
write_wrapper "$PREFIX/bin/oracle-cli" "$PREFIX/bin/oracle-cli-bin" "$VERSION"

install -m755 "$WORK_DIR/oracle-update" "$PREFIX/bin/oracle-update"

if [ "$INSTALL_SERVICE" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
    USER_UNITS="$CONFIG_HOME/systemd/user"
    mkdir -p "$USER_UNITS"
    sed \
        -e "s|@VENV@|$VENV|g" \
        -e "s|@ENV_FILE@|$ENV_FILE|g" \
        -e "s|@DATA_DIR@|$DATA_DIR|g" \
        "$WORK_DIR/oracle-daedalus.service" > "$USER_UNITS/oracle-daedalus.service"
    if [ "$INSTALL_UPDATER" -eq 1 ]; then
        sed -e "s|@PREFIX@|$PREFIX|g" \
            "$WORK_DIR/oracle-daedalus-update.service" \
            > "$USER_UNITS/oracle-daedalus-update.service"
        install -m644 "$WORK_DIR/oracle-daedalus-update.timer" \
            "$USER_UNITS/oracle-daedalus-update.timer"
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now oracle-daedalus.service
    if [ "$INSTALL_UPDATER" -eq 1 ]; then
        systemctl --user enable --now oracle-daedalus-update.timer
    fi
fi

printf '\nInstalled. Start the console with: %s/bin/oracle\n' "$PREFIX"
printf 'Your libraries remain in: %s\n' "$DATA_DIR"
