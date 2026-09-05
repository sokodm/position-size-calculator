#!/bin/sh
# Double-click launcher for macOS. Hands straight off to run.py, which does the
# real work; this file exists only because Finder will not run a .py on a
# double-click. `dirname $0` keeps it working from any folder, including one
# with spaces in the path.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed on this Mac."
    echo
    echo "Install it from https://www.python.org/downloads/ (or run: xcode-select --install)"
    echo "then double-click this file again."
    echo
    printf "Press Return to close ... "
    read -r _
    exit 1
fi

python3 run.py
status=$?

# On a failure the Terminal window would otherwise close instantly and take the
# error message with it.
if [ "$status" -ne 0 ]; then
    echo
    printf "Something went wrong (exit %s). Press Return to close ... " "$status"
    read -r _
fi
exit "$status"
