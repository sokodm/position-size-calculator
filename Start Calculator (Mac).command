#!/bin/sh
# Double-click launcher for macOS. Hands off to run.py, which installs the
# Python libraries and starts the app; this file's own job is to make sure a
# Python 3.10+ exists to run it with, downloading a private one if the Mac has
# none. `dirname $0` keeps it working from any folder, including one with
# spaces in the path.
cd "$(dirname "$0")" || exit 1

# A browser-downloaded ZIP arrives with every file flagged com.apple.quarantine,
# and macOS hard-blocks a flagged unsigned script on double-click -- the dialog
# offers only Move to Trash or Done, and neither one runs it. Being allowed to
# run does not mean the flag is gone: "Open Anyway" records a Gatekeeper
# exception for this one file and commonly leaves the flag in place, and running
# the file from Terminal skips the check entirely. So clear it folder-wide,
# which is what stops the next double-click hitting the same wall. Guarded
# because the recursive walk would otherwise cross .venv (~435 MB) on every
# launch; a folder with no flag, including any git clone, skips it. basename
# rather than "$0" because the cd above has already moved, and a relative $0
# would no longer resolve. Unlike python3, xattr is not one of the xcrun-gated
# stubs, so it cannot raise the developer-tools prompt that
# safe_to_probe_system_python() exists to dodge.
if xattr -p com.apple.quarantine "$(basename "$0")" >/dev/null 2>&1; then
    # Exit status is 0 when files simply have no flag, so a failure here is real.
    if ! xattr -dr com.apple.quarantine . 2>/dev/null; then
        echo "Note: macOS's download flag could not be cleared from this folder."
        echo "The app still starts, but macOS may block this file again next"
        echo "time. The README's \"Mac: macOS blocked the start file\" section"
        echo "has the fix."
        echo
    fi
fi

PY_VERSION="3.12.14"
PY_BUILD="20260901"
RUNTIME_DIR=".runtime"
RUNTIME_PY="$RUNTIME_DIR/python/bin/python3"

case "$(uname -m)" in
    arm64)
        PY_TRIPLE="aarch64-apple-darwin"
        PY_SHA256="3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"
        ;;
    x86_64)
        PY_TRIPLE="x86_64-apple-darwin"
        PY_SHA256="2e31b23f3f1319f707d0e620b48847a0046577541d357276821f9f1b5492e0ba"
        ;;
    *)
        PY_TRIPLE=""
        PY_SHA256=""
        ;;
esac

pause_then_exit() {
    echo
    printf "Press Return to close ... "
    read -r _
    exit "$1"
}

# 3.10 is run.py's floor. Checking the version rather than mere existence
# matters on older Macs, which still ship a python3 that is too old.
#
# The 3.14 ceiling is not arbitrary and must not be raised on its own:
# tradingview-mcp-server==0.8.1 declares Requires-Python >=3.10,<3.14, so on a
# newer Python this check would pass, a venv would be built, and pip would only
# then refuse to install -- long after the interpreter was chosen. Failing the
# probe instead sends a too-new Mac down the download path to the private 3.12,
# which is exactly what that path exists for.
usable() {
    "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)' \
        >/dev/null 2>&1
}

# A Mac with no Xcode command line tools still has /usr/bin/python3, but it is
# a stub: running it pops a blocking "install developer tools" dialog and waits.
# That is the factory-fresh Mac this launcher most needs to serve, so check for
# the tools without executing the stub and let it fall through to the download.
# Any python3 from elsewhere (python.org, Homebrew) is a real one -- probe it.
safe_to_probe_system_python() {
    resolved=$(command -v python3) || return 1
    [ "$resolved" = "/usr/bin/python3" ] || return 0
    xcode-select -p >/dev/null 2>&1
}

download_python() {
    if [ -z "$PY_TRIPLE" ]; then
        echo "This Mac's processor ($(uname -m)) is not one we have a Python"
        echo "download for. Install Python yourself from"
        echo "https://www.python.org/downloads/ and run this file again."
        return 1
    fi

    archive="cpython-${PY_VERSION}+${PY_BUILD}-${PY_TRIPLE}-install_only.tar.gz"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_BUILD}/${archive}"
    staging="$RUNTIME_DIR/.download"

    echo "This Mac does not have Python, so the app will download its own copy"
    echo "(about 24 MB). It goes in this folder only -- nothing is installed"
    echo "system-wide, and no password is needed."
    echo

    rm -rf "$staging" || return 1
    mkdir -p "$staging" || return 1

    if ! curl -fL --progress-bar -o "$staging/$archive" "$url"; then
        echo
        echo "The download failed. Check your internet connection and try again."
        return 1
    fi

    got=$(shasum -a 256 "$staging/$archive" | awk '{print $1}')
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
# interpreters between runs would leave that .venv unusable.
if [ -x "$RUNTIME_PY" ] && usable "$RUNTIME_PY"; then
    PYTHON="$RUNTIME_PY"
elif safe_to_probe_system_python && usable python3; then
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

# On a failure the Terminal window would otherwise close instantly and take the
# error message with it.
if [ "$status" -ne 0 ]; then
    echo
    printf "Something went wrong (exit %s). Press Return to close ... " "$status"
    read -r _
fi
exit "$status"
