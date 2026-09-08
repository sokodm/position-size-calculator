#!/bin/sh
# Terminal launcher for Linux. Hands off to run.py, which installs the Python
# libraries and starts the app; this file's own job is to make sure a Python
# 3.10-3.13 that can build a venv exists to run it with, downloading a private
# one if the machine has none. `dirname $0` keeps it working from any folder,
# including one with spaces in the path.
#
# Adapted from "Start Calculator (Mac).command" and deliberately standalone.
# Every launcher has to work from a partly-extracted folder, so none of them
# sources a shared file; CI guards the duplication instead, by asserting all
# three pin the same CPython version and release.
cd "$(dirname "$0")" || exit 1

PY_VERSION="3.12.14"
PY_BUILD="20260901"
RUNTIME_DIR=".runtime"
RUNTIME_PY="$RUNTIME_DIR/python/bin/python3"
LAUNCHER_NAME="Start Calculator (Linux).sh"

case "$(uname -m)" in
    x86_64)
        PY_TRIPLE="x86_64-unknown-linux-gnu"
        PY_SHA256="72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c"
        PY_SIZE="33 MB"
        ;;
    aarch64|arm64)
        PY_TRIPLE="aarch64-unknown-linux-gnu"
        PY_SHA256="577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d"
        PY_SIZE="28 MB"
        ;;
    *)
        PY_TRIPLE=""
        PY_SHA256=""
        PY_SIZE=""
        ;;
esac

pause_then_exit() {
    echo
    printf "Press Return to close ... "
    read -r _
    exit "$1"
}

# A double-clicked .sh usually gets no terminal: GNOME Files opens executable
# text files in an editor unless the user changes a preference, and a script
# started from a file manager has nowhere to print -- so every message below,
# including the download progress and the failure text, would go nowhere.
# Rather than fail invisibly, find a terminal and start again inside it.
#
# PSC_NO_REEXEC=1 means "do not go looking for a terminal". The re-exec sets it,
# which is what stops a loop; CI and container tests set it because their stdin
# and stdout are redirected and a runner has no DISPLAY, so without it every
# launcher step would test this refusal path instead of the launcher.
if [ "$PSC_NO_REEXEC" != "1" ] && [ ! -t 0 ] && [ ! -t 1 ]; then
    if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
        export PSC_NO_REEXEC=1
        self="$PWD/$LAUNCHER_NAME"
        # Each emulator has its own way of being handed a command, and the
        # names are tried in order of how likely a desktop is to have one.
        for term in x-terminal-emulator gnome-terminal konsole xfce4-terminal \
                    kitty alacritty foot xterm; do
            command -v "$term" >/dev/null 2>&1 || continue
            case "$term" in
                gnome-terminal) exec "$term" -- "$self" ;;
                xfce4-terminal) exec "$term" -x "$self" ;;
                kitty|foot)     exec "$term" "$self" ;;
                *)              exec "$term" -e "$self" ;;
            esac
        done
    fi
    echo "The Position Size Calculator needs a terminal to run in, and this"
    echo "start was not given one -- most Linux desktops open a double-clicked"
    echo "script in a text editor instead of running it."
    echo
    echo "Please open a terminal in this folder and run:"
    echo
    echo "    ./\"$LAUNCHER_NAME\""
    echo
    exit 1
fi

# 3.10 is run.py's floor.
#
# The 3.14 ceiling is not arbitrary and must not be raised on its own:
# tradingview-mcp-server==0.8.1 declares Requires-Python >=3.10,<3.14, so on a
# newer Python this check would pass, a venv would be built, and pip would only
# then refuse to install -- long after the interpreter was chosen. Failing the
# probe instead sends a too-new machine down the download path to the private
# 3.12, which is exactly what that path exists for.
usable() {
    "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
        >/dev/null 2>&1
}

# Debian and Ubuntu ship python3 without python3-venv, so a version check
# passes and `python3 -m venv` then fails. No import-based probe detects this:
# venv is stdlib and imports fine, what Debian strips is ensurepip's bundled
# wheels, and `import ensurepip` can itself succeed while ensurepip.bootstrap()
# fails. Doing the real thing is the only trustworthy answer.
#
# It costs a few seconds on first launch only: once run.py's .venv exists, its
# existence is the answer.
can_make_venv() {
    [ -d .venv ] && return 0
    probe=$(mktemp -d) || return 1
    "$1" -m venv "$probe/v" >/dev/null 2>&1
    rc=$?
    rm -rf "$probe"
    return "$rc"
}

# Minimal images commonly ship one of these and not the other.
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fL --progress-bar -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$2" "$1"
    else
        echo "Neither curl nor wget is installed, so the Python download cannot"
        echo "run. Install either one, or install python3 from your package"
        echo "manager -- see the README's \"Installing Python\" section."
        return 1
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    fi
}

# ldd writes its version to stderr on glibc and stdout on musl, so both are
# captured.
is_musl() {
    ldd --version 2>&1 | grep -qi musl
}

download_python() {
    # A glibc tarball cannot execute on musl, so say so before spending 33 MB
    # finding out.
    if is_musl; then
        echo "This system uses musl (Alpine and similar). The private Python"
        echo "download is built for glibc and would not run here, so install"
        echo "python3 from your package manager instead:"
        echo
        echo "    sudo apk add python3"
        echo
        echo "then run this file again."
        return 1
    fi

    if [ -z "$PY_TRIPLE" ]; then
        echo "This machine's processor ($(uname -m)) is not one we have a Python"
        echo "download for. Install python3 from your package manager and run"
        echo "this file again."
        return 1
    fi

    archive="cpython-${PY_VERSION}+${PY_BUILD}-${PY_TRIPLE}-install_only_stripped.tar.gz"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_BUILD}/${archive}"
    staging="$RUNTIME_DIR/.download"

    echo "This computer does not have a Python the app can use, so it will"
    echo "download its own copy (about $PY_SIZE). It goes in this folder only"
    echo "-- nothing is installed system-wide, and no password is needed."
    echo

    rm -rf "$staging" || return 1
    mkdir -p "$staging" || return 1

    if ! fetch "$url" "$staging/$archive"; then
        echo
        echo "The download failed. Check your internet connection and try again."
        return 1
    fi

    got=$(sha256_of "$staging/$archive")
    if [ -z "$got" ]; then
        echo
        echo "No checksum tool (sha256sum, shasum or openssl) is installed, so"
        echo "the download cannot be verified -- it will not be used. Install"
        echo "coreutils, or install python3 from your package manager."
        return 1
    fi
    if [ "$got" != "$PY_SHA256" ]; then
        echo
        echo "The downloaded Python does not match its expected checksum, so it"
        echo "will not be used."
        echo "  expected: $PY_SHA256"
        echo "  received: $got"
        return 1
    fi

    if ! tar -xzf "$staging/$archive" -C "$staging"; then
        echo
        echo "The Python download could not be unpacked."
        return 1
    fi

    rm -rf "$RUNTIME_DIR/python" || return 1
    mv "$staging/python" "$RUNTIME_DIR/python" || return 1
    rm -rf "$staging"

    usable "$RUNTIME_PY"
}

# A copy downloaded on an earlier run wins over the system Python: run.py's
# .venv is bound to whichever interpreter created it, so quietly switching
# interpreters between runs would leave that .venv unusable. The downloaded
# runtime skips the venv probe -- it always has a working ensurepip.
if [ -x "$RUNTIME_PY" ] && usable "$RUNTIME_PY"; then
    PYTHON="$RUNTIME_PY"
elif usable python3 && can_make_venv python3; then
    PYTHON="python3"
elif download_python; then
    PYTHON="$RUNTIME_PY"
else
    echo
    echo "The app could not get a working Python. The README's \"Installing"
    echo "Python\" section walks through doing it by hand."
    pause_then_exit 1
fi

"$PYTHON" run.py
status=$?

# In a re-exec'd terminal the window would otherwise close instantly and take
# the error message with it.
if [ "$status" -ne 0 ]; then
    echo
    printf "Something went wrong (exit %s). Press Return to close ... " "$status"
    read -r _
fi
exit "$status"
