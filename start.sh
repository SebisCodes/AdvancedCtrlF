#!/usr/bin/env bash
# =====================================================================
#  Advanced Ctrl+F - launcher for Linux and macOS
#  (on macOS double-click start.command, which runs this script)
#
#  Python is looked up in this order:
#    1. PYTHON_BIN (set below or as environment variable)
#    2. a Python inside the ./python/ subfolder
#    3. an archive in ./python/ (.tar.gz/.tgz/.tar.xz/.zip/.deb) -> extracted
#    4. download of the latest stable CPython (python-build-standalone,
#       SHA256-verified) into ./python/ - Linux and macOS, x86_64 and arm64
#    5. fallback: system python3 (in its own venv under ./python/venv)
#
#  Usage:  ./start.sh            start normally
#          ./start.sh --setup    set up only, do not start
#
#  Written for bash 3.2 (macOS default) and BSD tools as well as GNU/Linux.
# =====================================================================
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-}"     # e.g. PYTHON_BIN=/opt/python/bin/python3
PY_VERSION="${PY_VERSION:-}"     # e.g. PY_VERSION=3.13  (empty = latest stable)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_DIR="$ROOT/python"
RELEASE_API="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
mkdir -p "$PY_DIR" "$ROOT/data"

# Everything this launcher prints is also written to data/setup.log (kept below ~1 MB)
SETUP_LOG="$ROOT/data/setup.log"
if [ -f "$SETUP_LOG" ] && [ $(( $(wc -c <"$SETUP_LOG") )) -gt 1000000 ]; then mv -f "$SETUP_LOG" "$SETUP_LOG.1"; fi
if sed -u '' </dev/null >/dev/null 2>&1; then UNBUF="-u"; else UNBUF="-l"; fi   # GNU sed -u / BSD sed -l
exec > >(tee >(sed $UNBUF $'s/\033\\[[0-9;]*m//g' >>"$SETUP_LOG")) 2>&1   # log without colour codes
echo "===== $(date '+%Y-%m-%d %H:%M:%S') start.sh ${*:-} | $(uname -srm) | $ROOT"

say()  { printf '\033[1;34m[Advanced Ctrl+F]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[Advanced Ctrl+F] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

works() { [ -n "${1:-}" ] && [ -x "$1" ] && "$1" -I -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }

find_python() {
    local c
    for c in "$PY_DIR/bin/python3" "$PY_DIR/python/bin/python3" "$PY_DIR/venv/bin/python3"; do
        works "$c" && { echo "$c"; return 0; }
    done
    while IFS= read -r c; do
        works "$c" && { echo "$c"; return 0; }
    done < <(find "$PY_DIR" -maxdepth 6 \( -type f -o -type l \) \( -path '*/bin/python3' -o -path '*/bin/python3.[0-9]*' \) \
             ! -name '*-config' 2>/dev/null | sort)
    return 1
}

extract_archives() {
    local found=1 a
    shopt -s nullglob
    for a in "$PY_DIR"/*.tar.gz "$PY_DIR"/*.tgz "$PY_DIR"/*.tar.xz "$PY_DIR"/*.tar.bz2 "$PY_DIR"/*.zip "$PY_DIR"/*.deb; do
        found=0
        say "Extracting $(basename "$a") ..."
        case "$a" in
            *.zip) if command -v unzip >/dev/null; then unzip -q -o "$a" -d "$PY_DIR"
                   else tar -xf "$a" -C "$PY_DIR"; fi ;;
            *.deb) mkdir -p "$PY_DIR/deb"
                   if command -v dpkg-deb >/dev/null; then dpkg-deb -x "$a" "$PY_DIR/deb"
                   else ( tmp="$(mktemp -d)"; cd "$tmp" && ar x "$a" && tar -xf data.tar.* -C "$PY_DIR/deb"; rm -rf "$tmp" ); fi ;;
            *)     tar -xf "$a" -C "$PY_DIR" --no-same-owner ;;
        esac
        mkdir -p "$PY_DIR/_archive" && mv "$a" "$PY_DIR/_archive/"
    done
    shopt -u nullglob
    return $found
}

fetch() {  # fetch URL [target file]
    if command -v curl >/dev/null; then
        if [ -n "${2:-}" ]; then curl -fL --proto '=https' --tlsv1.2 --progress-bar -o "$2" "$1"
        else curl -fsSL --proto '=https' --tlsv1.2 -H 'User-Agent: AdvancedCtrlF' "$1"; fi
    elif command -v wget >/dev/null; then
        if [ -n "${2:-}" ]; then wget -q --show-progress --https-only -O "$2" "$1"
        else wget -q --https-only -O - "$1"; fi
    else
        fail "Neither curl nor wget found."
    fi
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

download_python() {
    local arch triple json urls url name sums expected actual tag dl
    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        *) say "Architecture $(uname -m) is not supported for automatic download."; return 1 ;;
    esac
    case "$(uname -s)" in
        Darwin) triple="${arch}-apple-darwin" ;;
        *)      triple="${arch}-unknown-linux-gnu" ;;
    esac
    say "Looking up the latest Python version ..."
    json="$(fetch "$RELEASE_API")" || return 1
    tag="$(printf '%s' "$json" | grep -o '"tag_name": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
    # Asset URLs encode "+" as "%2B"; skip pre-releases (a/b/rc) and free-threaded builds
    urls="$(printf '%s' "$json" | grep -o '"browser_download_url": *"[^"]*"' | sed 's/.*"\(https:[^"]*\)"$/\1/' \
            | grep -E "/cpython-[0-9]+\.[0-9]+\.[0-9]+(\+|%2B)[0-9]+-${triple}-install_only\.tar\.gz$" || true)"
    if [ -n "$PY_VERSION" ]; then
        urls="$(printf '%s\n' "$urls" | grep -E "/cpython-${PY_VERSION//./\\.}(\.|\+|%2B)" || true)"
    fi
    [ -n "$urls" ] || { say "No matching Python found in the release."; return 1; }
    url="$(printf '%s\n' "$urls" | sed -E 's#.*/cpython-([0-9.]+)(\+|%2B).*#\1 &#' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1 | cut -d' ' -f2)"
    name="$(basename "$url")"
    name="${name//%2B/+}"
    say "Downloading $name ..."
    dl="$PY_DIR/_download"
    rm -rf "$dl" && mkdir -p "$dl"
    fetch "$url" "$dl/python.tar.gz" || return 1
    sums="$(fetch "https://github.com/astral-sh/python-build-standalone/releases/download/${tag}/SHA256SUMS")" || return 1
    expected="$(printf '%s\n' "$sums" | grep -F "  $name" | head -1 | cut -d' ' -f1)"
    actual="$(sha256 "$dl/python.tar.gz")"
    [ -n "$expected" ] || { rm -rf "$dl"; fail "No checksum found for $name - aborting."; }
    [ "$expected" = "$actual" ] || { rm -rf "$dl"; fail "Checksum mismatch - download discarded."; }
    say "Checksum OK, extracting ..."
    tar -xzf "$dl/python.tar.gz" -C "$PY_DIR" --no-same-owner
    rm -rf "$dl"
}

system_venv() {
    local sys_py
    sys_py="$(command -v python3 || true)"
    works "$sys_py" || return 1
    say "Using system Python ($("$sys_py" --version)) in a dedicated venv."
    "$sys_py" -m venv "$PY_DIR/venv" || { say "Could not create venv (package python3-venv missing?)"; return 1; }
}

# ---------------------------------------------------------------- find Python
PY=""
if [ -n "$PYTHON_BIN" ]; then
    works "$PYTHON_BIN" && PY="$PYTHON_BIN" || fail "PYTHON_BIN=$PYTHON_BIN is not a working Python >= 3.10"
fi
[ -n "$PY" ] || PY="$(find_python || true)"
if [ -z "$PY" ] && extract_archives; then PY="$(find_python || true)"; fi
if [ -z "$PY" ]; then download_python && PY="$(find_python || true)" || true; fi
if [ -z "$PY" ]; then system_venv && PY="$(find_python || true)" || true; fi
[ -n "$PY" ] && say "Using Python: $PY ($("$PY" -I -c 'import sys; print(sys.version.split()[0])' 2>&1))"
[ -n "$PY" ] || fail "No Python found. Put a Python (or an archive) into $PY_DIR or set PYTHON_BIN."

# ---------------------------------------------------------------- packages + start
"$PY" -I "$ROOT/tools/ensure_deps.py" || fail "Setup failed (see above)."
[ "${1:-}" = "--setup" ] && { say "Setup complete."; exit 0; }
cd "$ROOT"
exec "$PY" -I "$ROOT/advanced_ctrl_f.py" ${@+"$@"}   # ${@+...}: bash 3.2 + set -u safe
