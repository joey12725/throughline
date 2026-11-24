#!/bin/sh
# Throughline Companion installer for Linux and macOS. Needs no GUI: run it
# in an SSH session, then the companion runs as a user service.
#
#   sh install.sh [--from SOURCE] [--extras LIST] [--] [setup arguments...]
#
# SOURCE: a folder of wheels (from `throughline-companion deploy`), a
# companion checkout path, or any pip/uv requirement (a git URL, an index
# name). Everything after the options is passed to
# `throughline-companion setup`, e.g.
#   --pair - --detected --capture-screens 60 --service
# A pairing code given as `--pair -` is read from stdin, so it never appears
# in the process list or the shell history.
set -eu

FROM=""
EXTRAS="capture,cookies,desktop-plugins"
UV_VERSION="0.13.9"

while [ $# -gt 0 ]; do
    case "$1" in
        --from) FROM="$2"; shift 2 ;;
        --extras) EXTRAS="$2"; shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

say() { printf '%s\n' "throughline-companion: $*" >&2; }

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then wget -q "$1" -O "$2"
    else say "needs curl or wget"; exit 1; fi
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

find_uv() {
    for candidate in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then printf '%s' "$candidate"; return 0; fi
    done
    return 1
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    os="$(uname -s)"; arch="$(uname -m)"
    case "$os-$arch" in
        Linux-x86_64) asset=uv-x86_64-unknown-linux-gnu.tar.gz; sum=a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5 ;;
        Linux-aarch64|Linux-arm64) asset=uv-aarch64-unknown-linux-gnu.tar.gz; sum=0797542018e541a9288855df555c8077eed06f7a47d206362030480e8ecc9f3d ;;
        Darwin-x86_64) asset=uv-x86_64-apple-darwin.tar.gz; sum=617a3211261b0fc739562f0071aa34e9739797c510208ede8e2977c30903d041 ;;
        Darwin-arm64) asset=uv-aarch64-apple-darwin.tar.gz; sum=5aeca67f2283f75235de40d0bd9c0a5bbc5fce23b6d48b3bf9fa1b2db08382ca ;;
        *) say "no uv build for $os $arch; install uv first"; exit 1 ;;
    esac
    tmp="$(mktemp -d)"
    say "installing uv $UV_VERSION"
    fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$asset" "$tmp/uv.tgz"
    if [ "$(sha256 "$tmp/uv.tgz")" != "$sum" ]; then say "uv download failed its checksum"; exit 1; fi
    tar -xzf "$tmp/uv.tgz" -C "$tmp"
    mkdir -p "$HOME/.local/bin"
    cp "$(find "$tmp" -type f -name uv | head -1)" "$HOME/.local/bin/uv"
    chmod 755 "$HOME/.local/bin/uv"
    rm -rf "$tmp"
    UV="$HOME/.local/bin/uv"
fi

if [ -z "$FROM" ]; then
    say "--from is required (a wheel folder, a checkout or a requirement)"; exit 2
fi
if [ -d "$FROM" ] && ls "$FROM"/throughline_companion-*.whl >/dev/null 2>&1; then
    say "installing from wheels in $FROM"
    "$UV" tool install --force --python 3.12 --find-links "$FROM" "throughline-companion[$EXTRAS]"
elif [ -d "$FROM" ]; then
    say "installing from $FROM"
    "$UV" tool install --force --python 3.12 "$FROM[$EXTRAS]"
else
    say "installing $FROM"
    "$UV" tool install --force --python 3.12 "throughline-companion[$EXTRAS] @ $FROM"
fi

BIN="$("$UV" tool dir --bin)/throughline-companion"
"$BIN" --version >/dev/null
say "installed $BIN"
if [ $# -gt 0 ]; then
    exec "$BIN" setup "$@"
fi
# A reinstall is an update: settings, pairing and connector state live in the
# per-user folders and are kept. Restart the service so the new version runs.
if "$BIN" service status 2>/dev/null | grep -q '"installed": true'; then
    "$BIN" service restart >/dev/null && say "service restarted on the new version"
fi
