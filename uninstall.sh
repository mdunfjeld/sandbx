#!/usr/bin/env bash
# Remove the `sandbx` symlink. Agent state and images are kept unless asked for.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/sandbx.py"
LEGACY_SRC="$REPO/sandbx"   # launcher name before the .py rename
PREFIX="${PREFIX:-$HOME/.local/bin}"
STATE="${XDG_DATA_HOME:-$HOME/.local/share}/sandbx"
FORCE=0 PURGE=0 IMAGES=0 ASSUME_YES=0

usage() {
    cat <<USAGE
usage: ./uninstall.sh [--prefix DIR] [--purge] [--images] [--force] [-y]

  --prefix DIR   where sandbx was installed (default: \$HOME/.local/bin)
  --purge        also delete $STATE
                 (every agent's stored credentials and session history)
  --images       also remove the podman images built by sandbx
  --force        remove DIR/sandbx even if it does not point at this checkout
  -y, --yes      skip confirmation prompts

By default this only removes the symlink. Your agent logins survive.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --purge) PURGE=1; shift ;;
        --images) IMAGES=1; shift ;;
        --force|-f) FORCE=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "uninstall: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    printf '%s [y/N] ' "$1"
    read -r reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ]
}

TARGET="$PREFIX/sandbx"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    current="$(readlink -f "$TARGET" || true)"
    if [ "$current" = "$SRC" ] || [ "$current" = "$LEGACY_SRC" ] || [ "$FORCE" -eq 1 ]; then
        rm -f "$TARGET"
        echo "removed: $TARGET"
    else
        echo "uninstall: $TARGET points at ${current:-nothing}, not this checkout;" >&2
        echo "           leaving it alone (use --force to remove anyway)" >&2
    fi
else
    echo "uninstall: nothing installed at $TARGET"
fi

if [ "$IMAGES" -eq 1 ]; then
    if command -v podman >/dev/null; then
        found="$(podman images --format '{{.Repository}}:{{.Tag}}' \
                 | grep -E '^(localhost/)?sandbx(-[a-z]+)?:' || true)"
        if [ -z "$found" ]; then
            echo "no sandbx images found"
        elif confirm "Remove these images?"$'\n'"$found"; then
            # shellcheck disable=SC2086
            podman rmi -f $found
        fi
    else
        echo "uninstall: podman not found; skipping images" >&2
    fi
fi

if [ "$PURGE" -eq 1 ]; then
    if [ -d "$STATE" ]; then
        echo "This deletes $STATE, including every agent's stored"
        echo "credentials and session history. This cannot be undone."
        if confirm "Delete it?"; then
            rm -rf "$STATE"
            echo "removed: $STATE"
        else
            echo "kept: $STATE"
        fi
    else
        echo "no state directory at $STATE"
    fi
fi
