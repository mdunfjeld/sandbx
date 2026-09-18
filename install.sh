#!/usr/bin/env bash
# Put `sandbx` on your PATH by symlinking it into a bin directory.
#
# A symlink rather than a copy, deliberately: the launcher locates images/ via
# its own resolved path, so a symlink keeps it working while letting edits in
# this checkout take effect immediately.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/sandbx.py"
LEGACY_SRC="$REPO/sandbx"   # launcher name before the .py rename
PREFIX="${PREFIX:-$HOME/.local/bin}"
FORCE=0

usage() {
    cat <<USAGE
usage: ./install.sh [--prefix DIR] [--force]

  --prefix DIR   install into DIR (default: \$HOME/.local/bin)
  --force        replace whatever already sits at DIR/sandbx

Installs a symlink, so this directory must stay where it is.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --force|-f) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "install: $*" >&2; exit 1; }

[ -f "$SRC" ] || die "no launcher at $SRC"
[ -x "$SRC" ] || chmod +x "$SRC"
command -v python3 >/dev/null || die "python3 not found; sandbx needs it"
command -v podman >/dev/null || echo "install: warning: podman not found on PATH" >&2

TARGET="$PREFIX/sandbx"
mkdir -p "$PREFIX"

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    current="$(readlink -f "$TARGET" || true)"
    if [ "$current" = "$SRC" ] || [ "$current" = "$LEGACY_SRC" ]; then
        : # already ours; re-link anyway so this stays idempotent
    elif [ "$FORCE" -eq 1 ]; then
        echo "install: replacing existing $TARGET"
    else
        die "$TARGET already exists (pointing at ${current:-nothing}); re-run with --force"
    fi
fi

ln -sfn "$SRC" "$TARGET"
"$TARGET" --help >/dev/null || die "installed, but $TARGET does not run"

echo "installed: $TARGET -> $SRC"

case ":$PATH:" in
    *":$PREFIX:"*)
        echo "run it from anywhere:  sandbx claude ~/project"
        ;;
    *)
        echo
        echo "warning: $PREFIX is not on your PATH. Add it:"
        echo "  echo 'export PATH=\"$PREFIX:\$PATH\"' >> ~/.bashrc && exec bash"
        ;;
esac
