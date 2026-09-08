# Linux Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Linux user can download the ZIP, run one command, and have the app start — with the same "you do not need to install Python first" promise the other two platforms make.

**Architecture:** `run.py` is already platform-neutral, so this adds a third self-contained launcher (`Start Calculator (Linux).sh`) that obtains a Python 3.10–3.13 capable of building a venv and hands it to `run.py`; teaches `app.py`'s stopped-app dialog a Linux branch; and extends CI and the README. Nothing shared is refactored — the duplication between launchers is guarded by a CI drift check rather than a common library, so the working Mac and Windows launchers stay byte-identical.

**Tech Stack:** POSIX `sh`, Python 3.10–3.13, Streamlit 1.62.0, `python-build-standalone` CPython 3.12.14 (release 20260901), GitHub Actions, Docker (local distro verification), Node 22 (test harness for the dialog's embedded JavaScript).

**Spec:** `docs/superpowers/specs/2026-09-08-linux-support-design.md`

## Global Constraints

- **Zero bytes changed** in `Start Calculator (Mac).command`, `Start Calculator (Windows).bat`, `Diagnose (Windows).bat`, `tools/`. Enforced by the Task 6 gate.
- Python range is **`>=3.10` inclusive, `<3.14` exclusive**. The ceiling exists because `tradingview-mcp-server==0.8.1` declares `Requires-Python >=3.10,<3.14`; a too-new interpreter must fail the probe and fall through to the download, never reach pip.
- Pinned interpreter: `PY_VERSION="3.12.14"`, `PY_BUILD="20260901"` — identical in all three launchers.
- Linux pins the **`install_only_stripped`** variant. Mac and Windows keep `install_only`.
  - `x86_64-unknown-linux-gnu` → `72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c` (32.6 MB)
  - `aarch64-unknown-linux-gnu` → `577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d` (27.8 MB)
- `PSC_NO_REEXEC=1` means "do not go looking for a terminal". The launcher sets it before re-exec (loop guard); CI and container steps set it because their stdin and stdout are redirected. Exactly one CI step leaves it unset, on purpose.
- A downloaded runtime always wins over the system Python: `run.py`'s `.venv` is bound to whichever interpreter created it.
- No comments that restate the code. Comments carry constraints and measurements only — the existing files set this bar; match it.
- Never `git add -A` / `git add .`. Stage the explicit paths each Commit step names.

---

### Task 1: The Linux launcher

**Files:**
- Create: `Start Calculator (Linux).sh` (committed mode 755)
- Test: Docker containers (no test file — the launcher's contract is "a real distro can start the app", which only a container can assert)

**Interfaces:**
- Consumes: `run.py` in the same folder, invoked as `"$PYTHON" run.py`.
- Produces: the file name `Start Calculator (Linux).sh`, referenced verbatim by Task 2 (`LINUX_FILE`), Task 3 (README), and Task 4 (CI). The variables `PY_VERSION` and `PY_BUILD` are read by Task 4's drift guard as `PY_VERSION="3.12.14"` / `PY_BUILD="20260901"` on their own lines.

- [ ] **Step 1: Write the launcher**

Create `Start Calculator (Linux).sh` with exactly this content:

```sh
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
```

- [ ] **Step 2: Make it executable and check its syntax**

```bash
chmod +x "Start Calculator (Linux).sh"
sh -n "Start Calculator (Linux).sh" && echo "syntax OK"
ls -l "Start Calculator (Linux).sh"
```

Expected: `syntax OK`, and the listing shows `-rwxr-xr-x`. If `shellcheck` is
installed, also run `shellcheck -s sh "Start Calculator (Linux).sh"` and fix
anything it reports; it is not a gate if absent.

- [ ] **Step 3: Verify the no-terminal refusal on the host**

This branch needs no Linux — it is reached whenever stdin and stdout are both
redirected and no display variable is set.

```bash
env -u DISPLAY -u WAYLAND_DISPLAY -u PSC_NO_REEXEC \
  ./"Start Calculator (Linux).sh" < /dev/null > /tmp/notty.txt 2>&1
echo "exit=$?"
cat /tmp/notty.txt
```

Expected: `exit=1`, and the output contains `Please open a terminal in this
folder and run:` followed by `./"Start Calculator (Linux).sh"`. On macOS this
is the only branch that is meaningful to test locally — everything below needs
a real Linux.

- [ ] **Step 4: Start Docker and build the container test harness**

```bash
open -a Docker
until docker info > /dev/null 2>&1; do osascript -e 'delay 3'; done
docker info --format 'docker ready: {{.ServerVersion}}'
```

Then create the harness. `run.py` has a `PSC_DIAGNOSE=1` mode that starts
Streamlit, waits until it genuinely serves, then stops — so each case asserts
the app **served**, not merely that a launcher exited 0.

```bash
mkdir -p /tmp/psc-linux-verify
cat > /tmp/psc-linux-verify/case.sh <<'EOF'
#!/bin/sh
# Runs inside the container. /src is the repo, mounted read-only; /app is a
# writable copy so .venv and .runtime do not land in the real checkout.
set -e
cp -r /src /app
cd /app
rm -rf .venv .runtime
export PSC_NO_REEXEC=1
export PSC_DIAGNOSE=1
# Assert on the string run.py prints only after Streamlit genuinely served
# (run.py:178), not on the exit code. An exit-0 check would also pass if
# run.py ever returned early, proving the launcher ran but not that the app
# came up -- which is the whole point of every case below.
./"Start Calculator (Linux).sh" 2>&1 | tee /tmp/case-out.txt
grep -q "Diagnostics: startup succeeded" /tmp/case-out.txt \
  || { echo "FAIL: the app did not serve"; exit 1; }
echo "SERVED OK"
EOF
chmod +x /tmp/psc-linux-verify/case.sh
```

- [ ] **Step 5: Case — no Python at all (ubuntu:24.04)**

```bash
cd "$(git rev-parse --show-toplevel)"
docker run --rm -v "$PWD:/src:ro" -v /tmp/psc-linux-verify:/h:ro ubuntu:24.04 \
  sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends \
         ca-certificates curl tar >/dev/null && /h/case.sh' 2>&1 | tail -30
```

Expected: the download message naming `about 33 MB`, then `run.py` proceeding
to install requirements, and finally the `PSC_DIAGNOSE` success line reporting
the app served. Non-zero exit is a failure.

- [ ] **Step 6: Case — Python present but no venv (debian:12-slim)**

This is the case no import-based probe would catch.

```bash
docker run --rm -v "$PWD:/src:ro" -v /tmp/psc-linux-verify:/h:ro debian:12-slim sh -c '
  set -eu
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    python3-minimal ca-certificates curl tar >/dev/null
  python3 --version
  # Assert the trap is actually set. If this image ever ships a working
  # ensurepip, the case stops being the interesting one, and it must fail
  # rather than quietly become a second system-Python test.
  if python3 -m venv /tmp/probe >/dev/null 2>&1; then
    echo "TRAP NOT SET: python3 -m venv works here"
    exit 1
  fi
  /h/case.sh
' 2>&1 | tail -30
```

Expected: `python3 --version` is 3.11 (so the version probe passes), the
launcher still reports the download, `.runtime/python/bin/python3` is what
`run.py` receives, and the app serves. If the launcher used the system Python
here, `run.py` would fail at venv creation — that failure is the regression
this case exists to catch.

- [ ] **Step 7: Case — Python with venv, and the second-run path (fedora:41)**

```bash
docker run --rm -v "$PWD:/src:ro" -v /tmp/psc-linux-verify:/h:ro fedora:41 \
  sh -c 'dnf install -y -q python3 >/dev/null && /h/case.sh && \
         echo "--- second run ---" && cd /app && PSC_NO_REEXEC=1 PSC_DIAGNOSE=1 \
         ./"Start Calculator (Linux).sh"' 2>&1 | tail -40
```

Expected: no download message, no `.runtime` directory created, app serves. The
second run reuses `.venv`, so `can_make_venv` returns immediately — the run
should visibly start faster and still serve.

- [ ] **Step 8: Case — musl (alpine:3)**

```bash
docker run --rm -v "$PWD:/src:ro" -v /tmp/psc-linux-verify:/h:ro alpine:3 \
  sh -c 'cp -r /src /app && cd /app && rm -rf .venv .runtime && \
         PSC_NO_REEXEC=1 ./"Start Calculator (Linux).sh"; echo "exit=$?"' 2>&1 | tail -20
```

Expected: `exit=1`, the message naming musl and `sudo apk add python3`, and
**no download attempted** — no `.runtime` directory and no network transfer in
the output.

- [ ] **Step 9: Case — bad checksum is refused**

```bash
docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 sh -c '
  apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    ca-certificates curl tar >/dev/null
  cp -r /src /app && cd /app && rm -rf .venv .runtime
  # Both hashes, not just the x86_64 one: the container runs as the host
  # architecture, so on an Apple Silicon machine this is aarch64 and the
  # launcher reads the aarch64 line. Corrupting only x86_64 would leave the
  # hash actually used intact -- the download would succeed and this case
  # would pass while proving nothing.
  sed -i "s/^\(        PY_SHA256=\)\".*\"/\1\"deadbeef\"/" \
    "Start Calculator (Linux).sh"
  test "$(grep -c deadbeef "Start Calculator (Linux).sh")" = 2 \
    || { echo "the sed did not corrupt both hashes"; exit 1; }
  PSC_NO_REEXEC=1 ./"Start Calculator (Linux).sh" < /dev/null; echo "exit=$?"
  test ! -e .runtime/python && echo "no interpreter was installed"
' 2>&1 | tail -20
```

Expected: both hashes corrupted (the `sed` guard passes), then the `does not
match its expected checksum` message showing expected and received, `exit=1`,
and `no interpreter was installed`. A tampered tarball must
never be unpacked into `.runtime/python`.

- [ ] **Step 10: Case — folder path containing spaces**

```bash
docker run --rm -v "$PWD:/src:ro" -v /tmp/psc-linux-verify:/h:ro ubuntu:24.04 \
  sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends \
         python3 python3-venv >/dev/null
         mkdir -p "/opt/my apps" && cp -r /src "/opt/my apps/psc"
         cd "/opt/my apps/psc" && rm -rf .venv .runtime
         PSC_NO_REEXEC=1 PSC_DIAGNOSE=1 ./"Start Calculator (Linux).sh"' 2>&1 | tail -20
```

Expected: the app serves. Every path in the launcher is quoted and every call
passes an argument list, so this should pass first time; it is cheap insurance
against a future unquoted `$PWD`.

- [ ] **Step 11: Commit**

```bash
git add "Start Calculator (Linux).sh"
git commit -m "Add a Linux start file

Same contract as the other two launchers: obtain a Python 3.10-3.13 and
hand it to run.py. Two Linux-only problems shape it.

A double-clicked .sh gets no terminal on most desktops, so with no tty it
re-execs inside the first emulator it finds and, failing that, prints the
command to run rather than dying silently.

Debian and Ubuntu ship python3 without python3-venv, and no import-based
probe detects that -- venv imports fine and ensurepip can import while
its bootstrap fails. So the probe builds a throwaway venv for real, on
first launch only."
```

---

### Task 2: The stopped-app dialog learns Linux

**Files:**
- Modify: `app.py:2392-2419` (the platform decision block) and `app.py:2474-2478` (the sentence)
- Create: `tests/test_stopped_dialog.py`

**Interfaces:**
- Consumes: `Start Calculator (Linux).sh` from Task 1, named verbatim.
- Produces: `tests/test_stopped_dialog.py`, added to the CI `test` job by Task 4 as `python tests/test_stopped_dialog.py`.

**Why a test and not a browser session:** the spec called for spoofing the
platform in a browser, recording the Mac and Windows output, and comparing
after. A Node harness over the same JavaScript proves the same thing, and keeps
proving it: the Mac and Windows sentences become asserted constants derived from
`origin/main`, so any later edit that changes them fails CI. Node 22 is present
locally and on all three GitHub runner images. The browser still gets exercised
— in Task 5, in a real Linux container, which additionally proves the dialog
renders.

- [ ] **Step 1: Capture the Mac and Windows baseline from `origin/main`**

This must happen before the edit; a baseline taken afterwards proves nothing.

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p /tmp/psc-baseline
git show origin/main:app.py > /tmp/psc-baseline/app-main.py
python3 - <<'PY'
import json, re, subprocess, tempfile, os
src = open("/tmp/psc-baseline/app-main.py").read()
# The decision block on main runs from `const nav` to the FILE_BROWSER line;
# the sentence is the argument of the first say(...) in the same script.
block = src[src.index("const nav = W.navigator;"):]
block = block[:block.index('": "your file browser";') + len('": "your file browser";')]
block = block.replace('""" + json.dumps(APP_DIR_NAME) + """',
                      '"position-size-calculator-main"')
m = re.search(r"say\(FILES\.length > 1(.*?)\);", src, re.S)
sentence = "(" + m.group(1).split("?", 1)[1] + ")"
harness = """
const W = {navigator: NAV};
%s
const INSTRUCTION = %s;
console.log(JSON.stringify({instruction: INSTRUCTION, files: FILES}));
""" % (block, sentence)
for name, nav in [
    ("windows", {"userAgentData": {"platform": "Windows"}, "platform": "Win32",
                 "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}),
    ("mac", {"userAgentData": {"platform": "macOS"}, "platform": "MacIntel",
             "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}),
]:
    js = harness.replace("NAV", json.dumps(nav))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js); path = f.name
    out = subprocess.run(["node", path], capture_output=True, text=True)
    os.unlink(path)
    print(name, "->", out.stdout.strip() or out.stderr.strip())
PY
```

Expected — record these two lines; they are the byte-identical target:

```
windows -> {"instruction":"To start it again, double-click this file in File Explorer:","files":["position-size-calculator-main\\Start Calculator (Windows).bat"]}
mac -> {"instruction":"To start it again, double-click this file in Finder:","files":["position-size-calculator-main/Start Calculator (Mac).command"]}
```

If the actual output differs from the above, **stop and report** — the plan's
expected constants are wrong and the rest of this task is built on them.

- [ ] **Step 2: Write the failing test**

Create `tests/test_stopped_dialog.py`:

```python
"""The stopped-app dialog's platform decision, run as real JavaScript.

The dialog is JavaScript embedded in a Python string, so nothing else in the
suite can reach it. This extracts the pure decision block -- everything from
the navigator sniff to the instruction sentence, which touches no DOM -- and
runs it under node against fake navigators.

The Windows and Mac expectations are the values origin/main produced before
Linux support was added. They are asserted rather than described: the point of
this file is that adding a third platform did not move the other two.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "app.py")

START = "// --- platform decision (extracted by tests/test_stopped_dialog.py) ---"
END = "// --- end platform decision ---"

FOLDER = "position-size-calculator-main"

WINDOWS = {
    "userAgentData": {"platform": "Windows"},
    "platform": "Win32",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
MAC = {
    "userAgentData": {"platform": "macOS"},
    "platform": "MacIntel",
    "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}
LINUX = {
    "userAgentData": {"platform": "Linux"},
    "platform": "Linux x86_64",
    "userAgent": "Mozilla/5.0 (X11; Linux x86_64)",
}
ANDROID = {
    "userAgentData": {"platform": "Android"},
    "platform": "Linux armv8l",
    "userAgent": "Mozilla/5.0 (Linux; Android 14; Pixel 8)",
}
UNKNOWN = {"platform": "", "userAgent": ""}

WIN_BOX = FOLDER + "\\Start Calculator (Windows).bat"
MAC_BOX = FOLDER + "/Start Calculator (Mac).command"
LINUX_BOX = './"Start Calculator (Linux).sh"'

CASES = [
    ("windows", WINDOWS,
     "To start it again, double-click this file in File Explorer:",
     [WIN_BOX]),
    ("mac", MAC,
     "To start it again, double-click this file in Finder:",
     [MAC_BOX]),
    ("linux", LINUX,
     "To start it again, open a terminal in the " + FOLDER + " folder and run:",
     [LINUX_BOX]),
    # A phone has no start file to run, and its UA says "Linux".
    ("android", ANDROID,
     "To start it again, double-click the file for your computer in "
     "your file browser:",
     [WIN_BOX, MAC_BOX, LINUX_BOX]),
    ("unknown", UNKNOWN,
     "To start it again, double-click the file for your computer in "
     "your file browser:",
     [WIN_BOX, MAC_BOX, LINUX_BOX]),
]


def decision_block():
    with open(APP, encoding="utf-8") as fh:
        src = fh.read()
    if START not in src or END not in src:
        raise AssertionError(
            "app.py no longer carries the extraction markers this test needs:\n"
            "  " + START + "\n  " + END
        )
    block = src[src.index(START) + len(START):src.index(END)]
    # The one Python splice inside the block.
    splice = '""" + json.dumps(APP_DIR_NAME) + """'
    if splice not in block:
        raise AssertionError("the APP_DIR_NAME splice is no longer in the block")
    return block.replace(splice, json.dumps(FOLDER))


def evaluate(block, nav):
    js = (
        "const W = {navigator: " + json.dumps(nav) + "};\n"
        + block
        + "\nconsole.log(JSON.stringify("
          "{instruction: INSTRUCTION, files: FILES}));\n"
    )
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        done = subprocess.run(
            ["node", path], capture_output=True, text=True
        )
    finally:
        os.unlink(path)
    if done.returncode != 0:
        raise AssertionError("node could not run the block:\n" + done.stderr)
    return json.loads(done.stdout)


def main():
    if subprocess.run(
        ["node", "--version"], capture_output=True
    ).returncode != 0:
        # Not skipped: without node this file asserts nothing, and a test that
        # silently asserts nothing is worse than a missing one.
        print("FAIL  node is not installed, so the dialog block cannot be run")
        return 1

    block = decision_block()
    failures = 0
    for name, nav, instruction, files in CASES:
        got = evaluate(block, nav)
        if got["instruction"] != instruction:
            print("FAIL  " + name + " instruction")
            print("      expected: " + instruction)
            print("      received: " + got["instruction"])
            failures += 1
        if got["files"] != files:
            print("FAIL  " + name + " files")
            print("      expected: " + json.dumps(files))
            print("      received: " + json.dumps(got["files"]))
            failures += 1
        if not failures:
            print("ok    " + name)
    if failures:
        print("\n%d assertion(s) failed" % failures)
        return 1
    print("\nall %d platforms produce the expected dialog" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run it to make sure it fails**

```bash
python3 tests/test_stopped_dialog.py; echo "exit=$?"
```

Expected: `exit=1` with the message about the missing extraction markers —
`app.py` has not been touched yet.

- [ ] **Step 4: Add the markers and the Linux branch to `app.py`**

Replace lines 2392–2419 (from `const nav = W.navigator;` through the
`FILE_BROWSER` definition) so the region is marker-delimited and gains Linux.
The three comment blocks already above `const nav`, `const FOLDER` and
`const FILE_BROWSER` stay exactly as they are — only add what is shown here.

Insert immediately **before** `const nav = W.navigator;`:

```javascript
        // --- platform decision (extracted by tests/test_stopped_dialog.py) ---
        // Everything to the end marker is pure: it reads navigator and builds
        // strings, touching no DOM, which is what lets a test run it under
        // node. Keep it that way -- move DOM work below the marker.
```

Add after the existing `const IS_MAC = ...` line:

```javascript
        // Android's user agent also says "Linux", and a phone has no start file
        // to run, so it must not take the Linux branch -- it belongs on the
        // fallback that lists all three.
        const IS_LINUX = !IS_WIN && !IS_MAC && !/Android/i.test(ua)
            && (/Linux/i.test(plat) || /X11/i.test(ua));
```

Add after the existing `const MAC_FILE = ...` line:

```javascript
        // A command to type, not a path to point at: the file name has spaces,
        // so an unquoted ./Start Calculator (Linux).sh is a shell error rather
        // than a start. Single-quoted here purely so the inner double quotes
        // need no escaping. The folder moves into the sentence instead, since a
        // terminal is already inside it by the time this is run.
        const LINUX_FILE = './"Start Calculator (Linux).sh"';
```

Replace the `FILES` definition with:

```javascript
        const FILES = IS_WIN ? [WIN_FILE]
            : IS_MAC ? [MAC_FILE]
                : IS_LINUX ? [LINUX_FILE]
                    : [WIN_FILE, MAC_FILE, LINUX_FILE];
```

Then, immediately after the existing `const FILE_BROWSER = ...` line, add the
sentence and the end marker:

```javascript
        // Explicit per platform, not derived from FILES.length. The count used
        // to imply the platform -- one file meant "recognised", two meant "not"
        // -- and Linux breaks that inference: it has one file and still needs
        // its own sentence, because a .sh is not something a double-click can
        // be promised to run.
        const INSTRUCTION = IS_WIN || IS_MAC
            ? "To start it again, double-click this file in "
              + FILE_BROWSER + ":"
            : IS_LINUX
                ? "To start it again, open a terminal in the " + FOLDER
                  + " folder and run:"
                : "To start it again, double-click the file for your computer in "
                  + FILE_BROWSER + ":";
        // --- end platform decision ---
```

**Do not** add a Linux arm to `FILE_BROWSER`. The spec asked for `"your file
manager"`, but the Linux sentence names a terminal and never interpolates
`FILE_BROWSER`, so that arm would be dead code. The existing `"your file
browser"` fallback still serves the unrecognised branch.

- [ ] **Step 5: Replace the derived sentence at the `say(...)` call**

Replace these five lines (currently `app.py:2474-2478`):

```javascript
            say(FILES.length > 1
                ? "To start it again, double-click the file for your computer in "
                  + FILE_BROWSER + ":"
                : "To start it again, double-click this file in "
                  + FILE_BROWSER + ":");
```

with:

```javascript
            say(INSTRUCTION);
```

The comment block directly above it ("One instruction and one reassurance…")
stays — it explains the dialog's copy, which has not changed.

- [ ] **Step 6: Run the test to verify it passes**

```bash
python3 tests/test_stopped_dialog.py; echo "exit=$?"
```

Expected: `ok` for all five platforms, `all 5 platforms produce the expected
dialog`, `exit=0`. The `windows` and `mac` lines passing is the non-regression
proof: those two strings came from `origin/main` in Step 1.

- [ ] **Step 7: Confirm the app still byte-compiles and its own suite passes**

```bash
python3 -m compileall -q app.py run.py tests && echo "compile OK"
python3 tests/test_state.py
```

Expected: `compile OK`, and `test_state.py` passing as before. It does not
cover this code — it is here to catch a mangled edit to a 230 KB file.

- [ ] **Step 8: Commit**

```bash
git add app.py tests/test_stopped_dialog.py
git commit -m "Teach the stopped-app dialog about Linux

The instruction sentence was derived from FILES.length, which worked only
because one file implied a recognised platform. Linux breaks that: one
file, but a terminal command rather than a double-click. The sentence is
now explicit per platform.

The new test runs the decision block as real JavaScript under node. The
Windows and Mac expectations are the strings origin/main produced, so
the file's job is to fail if a third platform ever moves the first two."
```

---

### Task 3: Documentation

**Files:**
- Modify: `run.py:2` (docstring), `README.md`, `CONTRIBUTING.md:47`

**Interfaces:**
- Consumes: the launcher file name and download sizes from Task 1; `tests/test_stopped_dialog.py` is not mentioned in the README (it is a contributor-facing file, and CONTRIBUTING already points at the suite).
- Produces: nothing later tasks read.

- [ ] **Step 1: `run.py` docstring**

Change line 2 from:

```
"""One-command launcher for the Position Size Calculator (macOS + Windows).
```

to:

```
"""One-command launcher for the Position Size Calculator (macOS, Windows and Linux).
```

- [ ] **Step 2: README — the three sentences that are false on Linux**

In the `## Install it` opening paragraph, after "…it all stays inside the folder
you unzipped." append:

```markdown
(On Linux it is one command in a terminal rather than a double-click; see
step 3.)
```

Change the step 3 lead-in from:

```markdown
**3. Start it** — open the unzipped folder and double-click:
```

to:

```markdown
**3. Start it** — open the unzipped folder and start the file for your system:
```

Change the private-Python sizes line from:

```markdown
private copy (24 MB on Mac, 45 MB on Windows) into the folder before carrying
```

to:

```markdown
private copy (24 MB on Mac, 45 MB on Windows, 33 MB on Linux) into the folder
before carrying
```

- [ ] **Step 3: README — the additive per-OS bullets**

In **2. Unzip**, add a third bullet after the Windows one:

```markdown
- **Linux** — double-click the ZIP file, or run
  `unzip position-size-calculator-main.zip` in a terminal.
```

In **3. Start it**, add a third bullet after the Windows one:

```markdown
- **Linux** — open a terminal in the folder and run:

  ```sh
  ./"Start Calculator (Linux).sh"
  ```

  Double-clicking it does work on some Linux desktops, but many open it in a
  text editor instead. The command above always works.
```

- [ ] **Step 4: README — the new Linux troubleshooting section**

Insert after the whole `### Windows: getting a diagnosis` section (i.e. after
the "Neither file leaves your computer; both are ignored by Git." paragraph)
and before `### Installing Python`:

```markdown
### Linux: if it does not start

| Message | Fix |
|---|---|
| Double-clicking the start file opens it in a text editor | That is your desktop's setting for executable text files, not a fault. Start it from a terminal instead — step 3. |
| `Permission denied` | Your unzip tool dropped the file's executable flag. Run `chmod +x "Start Calculator (Linux).sh"` once, then start it again. |
| It downloads its own Python even though `python3` is installed | Expected on Debian and Ubuntu, where `python3` ships without the `venv` piece the app needs. It fetches its own copy rather than fail halfway through. Nothing to fix — or install `python3-venv` if you would rather it used yours. |
| "not one we have a Python download for", or a message about musl | Your machine is not x86-64 or ARM64, or it uses musl rather than glibc (Alpine). Install Python from your package manager — see [Installing Python](#installing-python). |
```

**The existing `### If it does not start` table must not be touched.** Its two
generic rows already read correctly on Linux, and leaving it alone is what makes
the Task 6 gate a diff of nothing.

- [ ] **Step 5: README — Installing Python, and the Git route**

In `### Installing Python`, after the Windows numbered list, add:

```markdown
**Linux**

Use your distribution's package manager. On Debian or Ubuntu the `venv` package
matters as much as Python itself — without it the app cannot build its private
environment, and will download its own Python instead:

```sh
sudo apt install python3 python3-venv     # Debian, Ubuntu, Mint
sudo dnf install python3                  # Fedora
sudo pacman -S python                     # Arch
```

Then run `./"Start Calculator (Linux).sh"` again.
```

In `### Advanced: install with Git`, change:

```markdown
That bootstrap lives in the two start files.
```

to:

```markdown
That bootstrap lives in the three start files.
```

and change:

```markdown
Then, on **Mac**:
```

to:

```markdown
Then, on **Mac** or **Linux**:
```

- [ ] **Step 6: README and CONTRIBUTING — the platform sentences**

Change README's CI sentence from:

```markdown
`test_state.py` runs in CI on macOS and Windows — the two platforms the
launchers target — against Python 3.10 (the floor this project supports) and
```

to:

```markdown
`test_state.py` runs in CI on macOS, Windows and Linux — the three platforms the
launchers target — against Python 3.10 (the floor this project supports) and
```

Change `CONTRIBUTING.md:47` from:

```markdown
- **The launcher must keep working on both macOS and Windows.** Everything in
```

to:

```markdown
- **The launchers must keep working on macOS, Windows and Linux.** Everything in
```

These last two were found while planning and are not named in the spec; they are
the same "two platforms" claim in two more places, and leaving them would make
the docs contradict the CI matrix Task 4 adds.

- [ ] **Step 7: Check no machine-local path crept in**

```bash
git diff -- README.md CONTRIBUTING.md run.py | grep -nE '~/|/Users/|C:\\Users|dsokolovsky' || echo "no local paths"
```

Expected: `no local paths`.

- [ ] **Step 8: Verify the rendered README structure**

```bash
grep -n "^#\{2,3\} " README.md
```

Expected: `### Linux: if it does not start` appears between
`### Windows: getting a diagnosis` and `### Installing Python`, and no other
heading moved.

- [ ] **Step 9: Commit**

```bash
git add README.md CONTRIBUTING.md run.py
git commit -m "Document the Linux route

Linux joins the per-OS bullet lists that already exist, and gets its own
troubleshooting section rather than four more rows in the shared table --
the README already scopes a section per platform for Windows diagnostics,
and three prefixed rows would make the first thing a struggling Mac user
reads mostly about Linux.

Three sentences changed because they were false on Linux as written: the
double-click promise, step 3's lead-in, and the download sizes."
```

---

### Task 4: CI

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `Start Calculator (Linux).sh` (Task 1) and `tests/test_stopped_dialog.py` (Task 2).
- Produces: nothing later tasks read.

- [ ] **Step 1: Add Linux to the two existing matrices**

In the `test` job, change:

```yaml
        os: [macos-latest, windows-latest]
```

to:

```yaml
        os: [macos-latest, windows-latest, ubuntu-latest]
```

and update the comment above it from "Both are launcher targets, so both get
tested." to "All three are launcher targets, so all three get tested."

In the `launcher` job, make the same `os:` change.

- [ ] **Step 2: Run the new dialog test in CI**

In the `test` job, after the `State, validation and sizing maths` step, add:

```yaml
      - name: The stopped-app dialog names the right file per platform
        # Runner images all ship node, which this needs: the dialog is
        # JavaScript inside a Python string, so it is run rather than parsed.
        run: python tests/test_stopped_dialog.py
```

- [ ] **Step 3: Add the two Linux launcher steps**

In the `launcher` job, after the `macOS -- downloads its own Python...` step,
add:

```yaml
      - name: Linux -- uses the Python already on the machine
        if: runner.os == 'Linux'
        shell: bash
        env:
          # stdin is redirected and stdout is piped, so neither is a tty, and a
          # runner has no DISPLAY. Without this the launcher would take the
          # no-terminal branch and this step would test the refusal path.
          PSC_NO_REEXEC: "1"
        run: |
          ./"Start Calculator (Linux).sh" < /dev/null | tee out.txt
          grep -q "RUNPY_VERSION=3." out.txt
          test ! -e .runtime

      - name: Linux -- downloads its own Python when the machine has none
        if: runner.os == 'Linux'
        shell: bash
        env:
          PSC_NO_REEXEC: "1"
        run: |
          mkdir -p shim
          printf '#!/bin/sh\nexit 1\n' > shim/python3
          chmod +x shim/python3
          PATH="$PWD/shim:$PATH" ./"Start Calculator (Linux).sh" < /dev/null | tee out2.txt
          grep -q "RUNPY_VERSION=3.12.14" out2.txt
          grep -q ".runtime" out2.txt
          test ! -e .runtime/.download

      - name: Linux -- refuses to run with no terminal and no display
        # The one step that deliberately leaves PSC_NO_REEXEC unset. With both
        # streams redirected and no display, the launcher must print the command
        # to run and fail, rather than fail silently.
        if: runner.os == 'Linux'
        shell: bash
        run: |
          set +e
          env -u DISPLAY -u WAYLAND_DISPLAY -u PSC_NO_REEXEC \
            ./"Start Calculator (Linux).sh" < /dev/null > notty.txt 2>&1
          rc=$?
          set -e
          cat notty.txt
          test "$rc" -ne 0
          grep -q "Please open a terminal in this folder and run:" notty.txt
```

- [ ] **Step 4: Make the hash check variant-aware and add the drift guard**

Replace the body of the `Pinned Python hashes still match the published release`
step (everything under `run: |`) with:

```yaml
        run: |
          set -euo pipefail
          mac="Start Calculator (Mac).command"
          win="Start Calculator (Windows).bat"
          lin="Start Calculator (Linux).sh"
          ver=$(sed -n 's/^PY_VERSION="\(.*\)"$/\1/p' "$mac")
          build=$(sed -n 's/^PY_BUILD="\(.*\)"$/\1/p' "$mac")
          echo "launchers pin CPython $ver from release $build"
          rc=0
          # The drift guard that stands in for a shared provisioning library:
          # three standalone launchers may duplicate the download logic, but
          # they may not disagree about what they are downloading.
          for f in "$win" "$lin"; do
            grep -qF "$ver" "$f" || { echo "FAIL  $f does not pin CPython $ver"; rc=1; }
            grep -qF "$build" "$f" || { echo "FAIL  $f does not pin release $build"; rc=1; }
          done
          curl -fsSL -o SHA256SUMS \
            "https://github.com/astral-sh/python-build-standalone/releases/download/$build/SHA256SUMS"
          # Linux pins install_only_stripped -- the same interpreter without
          # debug symbols, which takes its download from 106 MB to 33 MB -- so
          # the variant travels with the triple rather than being assumed.
          for spec in "aarch64-apple-darwin:$mac:install_only" \
                      "x86_64-apple-darwin:$mac:install_only" \
                      "x86_64-pc-windows-msvc:$win:install_only" \
                      "x86_64-unknown-linux-gnu:$lin:install_only_stripped" \
                      "aarch64-unknown-linux-gnu:$lin:install_only_stripped"; do
            triple=$(echo "$spec" | cut -d: -f1)
            file=$(echo "$spec" | cut -d: -f2)
            variant=$(echo "$spec" | cut -d: -f3)
            asset="cpython-$ver+$build-$triple-$variant.tar.gz"
            # Exact field match: a substring grep also matches every other
            # Python version's line for the same triple.
            published=$(awk -v a="$asset" '$2 == a { print $1 }' SHA256SUMS)
            if [ -z "$published" ]; then
              echo "FAIL  the release has no $asset"
              rc=1
            elif grep -qF "$published" "$file"; then
              echo "ok    $triple ($variant)"
            else
              echo "FAIL  $triple: $file does not pin $published"
              rc=1
            fi
          done
          exit $rc
```

Leave the step's `if: runner.os == 'macOS'` and its existing comment unchanged
— one runner still verifies every triple, and the reason (no Intel macOS runner
exists) has not changed.

- [ ] **Step 5: Add the no-venv container job**

After the `launcher` job's last step and before the `diagnostics` job, add:

```yaml
  launcher-linux-novenv:
    # The only job that can prove the venv fallthrough fires. debian:12-slim
    # with python3-minimal is the shape of a real Debian box: python3 exists and
    # passes the version check, but python3-venv is absent, so `python3 -m venv`
    # fails. The launcher must reject that interpreter and download its own --
    # if it accepted it, run.py would die building .venv.
    runs-on: ubuntu-latest
    container: debian:12-slim
    timeout-minutes: 15

    steps:
      - name: Install just enough to be the interesting case
        run: |
          set -eu
          apt-get update -qq
          apt-get install -y -qq --no-install-recommends \
            python3-minimal ca-certificates curl tar git
          python3 --version
          # Assert the trap is set. If a future image ships a working ensurepip
          # this job would quietly become a second system-Python test, and the
          # fallthrough would stop being covered anywhere.
          if python3 -m venv /tmp/probe >/dev/null 2>&1; then
            echo "python3 -m venv works here -- this job no longer tests what it claims"
            exit 1
          fi

      - uses: actions/checkout@v4

      - name: Stand in for run.py
        run: |
          cat > run.py <<'PY'
          import sys
          print("RUNPY_INTERPRETER=" + sys.executable)
          print("RUNPY_VERSION=%d.%d.%d" % sys.version_info[:3])
          PY

      - name: Rejects the system Python and downloads its own
        env:
          PSC_NO_REEXEC: "1"
        run: |
          set -eu
          ./"Start Calculator (Linux).sh" < /dev/null | tee out.txt
          grep -q "RUNPY_VERSION=3.12.14" out.txt
          test -x .runtime/python/bin/python3
          test ! -e .runtime/.download
```

- [ ] **Step 6: Validate the workflow parses**

```bash
python3 -c "import sys,yaml;d=yaml.safe_load(open('.github/workflows/ci.yml'));print('jobs:', list(d['jobs']));print('test os:', d['jobs']['test']['strategy']['matrix']['os']);print('launcher os:', d['jobs']['launcher']['strategy']['matrix']['os'])"
```

Expected: four jobs (`test`, `launcher`, `launcher-linux-novenv`,
`diagnostics`) and both matrices listing three OSes. If `yaml` is missing,
`python3 -m pip install pyyaml` first — it is a local check, not a project
dependency.

- [ ] **Step 7: Verify the macOS and Windows steps are untouched**

```bash
git diff .github/workflows/ci.yml | grep '^-' | grep -v '^---' | grep -vE 'os: \[|Both are launcher|set -euo|mac=|win=|ver=|build=|echo "launchers|curl -fsSL|for spec|triple=|file=|asset=|published=|if \[ -z|echo "FAIL|rc=1|elif grep|echo "ok|else|done|exit .rc|SHA256SUMS|# Exact field|fi'
```

Expected: no output. Every removed line belongs either to the two matrix lines,
the reworded comment, or the hash step being rewritten — nothing from a macOS or
Windows launcher step.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Cover Linux in CI

Additive: every existing step is already guarded by runner.os, so the
macOS and Windows steps run verbatim.

The hash check now carries the asset variant per triple, because Linux
pins install_only_stripped, and asserts all three launchers name the
same CPython version and release -- the drift guard that stands in for
the shared provisioning library these launchers deliberately do not have.

The debian:12-slim job is the only place the venv fallthrough is
provable, and it fails loudly if that image ever gains a working
ensurepip and stops being the interesting case."
```

---

### Task 5: The table-height question

**Files:**
- Investigate: `app.py:3966` (`grid_height`)
- Modify: only if the clipping reproduces

**Interfaces:**
- Consumes: a working Linux launcher (Task 1).
- Produces: either a reported finding of "does not reproduce", or a runtime
  measurement. Never padded constants.

`app.py` already records this risk in its own words: the AgGrid pixel height
adds no buffer for a horizontal scroll bar because macOS overlay scroll bars
take no layout height, and it is "UNVERIFIED on Windows/Linux classic
scrollbars, where ag-Grid gives it a real flex slot and the last row may clip."

- [ ] **Step 1: Serve the app from a Linux container with a browser attached**

```bash
cd "$(git rev-parse --show-toplevel)"
docker run --rm -d --name psc-linux -p 8765:8765 -v "$PWD:/src:ro" ubuntu:24.04 \
  sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends \
         ca-certificates curl tar >/dev/null
         cp -r /src /app && cd /app && rm -rf .venv .runtime
         PSC_NO_REEXEC=1 ./"Start Calculator (Linux).sh"'
docker logs -f psc-linux 2>&1 | head -40
```

Wait until the log shows Streamlit serving, then confirm from the host:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:8765
```

Expected: `200`.

- [ ] **Step 2: Add enough positions to make the grid scroll horizontally**

Drive `http://localhost:8765` with Playwright MCP (`mcp__playwright__*`). Set a
portfolio size, then add three positions so the table has three rows, and narrow
the viewport until the grid needs a horizontal scroll bar:

```
mcp__playwright__browser_resize  → width 900, height 800
```

- [ ] **Step 3: Measure whether the last row clips**

```javascript
// via mcp__playwright__browser_evaluate
() => {
  const grid = document.querySelector('.ag-root-wrapper');
  const body = document.querySelector('.ag-body-viewport');
  const rows = [...document.querySelectorAll('.ag-center-cols-container .ag-row')];
  const last = rows[rows.length - 1];
  return {
    gridHeight: grid && grid.getBoundingClientRect().height,
    viewportHeight: body && body.getBoundingClientRect().height,
    viewportScrollHeight: body && body.scrollHeight,
    hScrollbar: body ? body.offsetHeight - body.clientHeight : null,
    lastRowBottom: last && last.getBoundingClientRect().bottom,
    gridBottom: grid && grid.getBoundingClientRect().bottom,
  };
}
```

**Reproduces if** `hScrollbar > 0` **and** `lastRowBottom > gridBottom` (the
last row's bottom edge falls below the grid's own box), or
`viewportScrollHeight > viewportHeight` with a horizontal bar present.

- [ ] **Step 4a: If it does NOT reproduce — report and stop**

Record the measured numbers, take a screenshot for the record, and update the
comment at `app.py:3966` to replace "UNVERIFIED on Windows/Linux" with what was
actually measured on Linux, leaving Windows still marked unverified. Then:

```bash
git add app.py
git commit -m "Record the measured Linux scrollbar behaviour

The comment said UNVERIFIED on Windows and Linux. Linux is now measured
in a container against classic scrollbars and the last row does not
clip, so only the Windows half of that warning still stands."
```

A null result is the end of this task. **Do not** change `grid_height` because
clipping seems plausible.

- [ ] **Step 4b: If it DOES reproduce — measure at runtime, never pad**

`GRID_HEADER_HEIGHT` and `GRID_ROW_HEIGHT` are shared by all three platforms,
and the same comment records that padding them recreates a blank-strip bug. The
fix must be a measurement that is a no-op where the scroll bar takes no layout
height, which is macOS. Add to the grid's JS options, alongside the existing
grid setup:

```javascript
// A classic scroll bar (Linux, Windows) takes a real flex slot inside the
// grid and pushes the last row past the fixed pixel height; a macOS overlay
// scroll bar takes none, so measuring gives 0 there and this is inert.
// Padding GRID_ROW_HEIGHT instead would add the strip on every platform --
// which is the blank-strip bug that comment warns about.
onFirstDataRendered: (e) => {
  const v = document.querySelector('.ag-body-viewport');
  const bar = v ? v.offsetHeight - v.clientHeight : 0;
  if (bar > 0) {
    e.api.setGridOption('domLayout', 'normal');
    const root = document.querySelector('.ag-root-wrapper');
    if (root) { root.style.height = (root.offsetHeight + bar) + 'px'; }
  }
}
```

Then re-run Steps 2–3 and confirm `lastRowBottom <= gridBottom`, **and** re-run
the same measurement on the host macOS browser to confirm `bar === 0` and the
grid height is unchanged from before. Commit only with both results recorded.

- [ ] **Step 5: Tear down the container**

```bash
docker rm -f psc-linux
docker ps --filter name=psc-linux
```

Expected: no rows.

---

### Task 6: Non-regression gate, local Mac re-run, and PR

**Files:** none modified — this task only verifies and opens the PR.

**Interfaces:**
- Consumes: everything from Tasks 1–5.

- [ ] **Step 1: The zero-bytes gate**

```bash
cd "$(git rev-parse --show-toplevel)"
git fetch origin
git diff origin/main --stat -- \
  "Start Calculator (Mac).command" \
  "Start Calculator (Windows).bat" \
  "Diagnose (Windows).bat" \
  tools/
echo "gate exit=$?"
```

Expected: **no output** before `gate exit=0`. Any line here fails the user's
explicit requirement — stop and report rather than arguing the change is
harmless.

- [ ] **Step 2: Re-run the Mac launcher locally, both paths**

The runtime has not been swapped, but these two paths are what CI covers for
macOS and they are cheap to confirm by hand.

```bash
cd "$(git rev-parse --show-toplevel)"
cp run.py /tmp/run.py.real
cat > run.py <<'PY'
import sys
print("RUNPY_INTERPRETER=" + sys.executable)
print("RUNPY_VERSION=%d.%d.%d" % sys.version_info[:3])
PY
./"Start Calculator (Mac).command" < /dev/null | tee /tmp/mac1.txt
test ! -e .runtime && echo "no runtime created: ok"

mkdir -p /tmp/shim
printf '#!/bin/sh\nexit 1\n' > /tmp/shim/python3
chmod +x /tmp/shim/python3
PATH="/tmp/shim:$PATH" ./"Start Calculator (Mac).command" < /dev/null | tee /tmp/mac2.txt
grep -q "RUNPY_VERSION=3.12.14" /tmp/mac2.txt && echo "download path: ok"

cp /tmp/run.py.real run.py
rm -rf .runtime
git status --porcelain run.py
```

Expected: `RUNPY_VERSION=3.` in the first run with `no runtime created: ok`;
`download path: ok` in the second; and `git status --porcelain run.py` printing
nothing at the end, proving the stub was fully reverted. **If `run.py` shows as
modified, restore it before going further** — shipping the stub would break the
app for everyone.

- [ ] **Step 3: Run the whole local suite**

```bash
python3 -m compileall -q app.py run.py tests && echo "compile OK"
python3 tests/test_state.py
python3 tests/test_stopped_dialog.py
```

Expected: all three pass. `tests/test_lookup.py` stays out of this — it calls
TradingView, so run it by hand only if the search or refresh paths were touched
(they were not).

- [ ] **Step 4: Check the diff for machine-local paths**

```bash
git diff origin/main | grep -nE '~/|/Users/|C:\\Users|C:\\path\\to|dsokolovsky' || echo "no local paths"
```

Expected: `no local paths`.

- [ ] **Step 5: Confirm the new launcher's mode survived into git**

```bash
git ls-files -s "Start Calculator (Linux).sh" "Start Calculator (Mac).command"
```

Expected: both `100755`. A `100644` here means a Linux user's download would
arrive non-executable — fix with `git update-index --chmod=+x "Start Calculator (Linux).sh"`
and amend.

- [ ] **Step 6: Code review before the PR**

Dispatch `feature-dev:code-reviewer` **without** a `name` argument (a named
agent returns nothing and the review looks clean when it never ran). It has no
Bash tool, so pass it the file list and the diff content explicitly:

- `Start Calculator (Linux).sh` (new)
- `app.py` (dialog block only)
- `tests/test_stopped_dialog.py` (new)
- `.github/workflows/ci.yml`
- `README.md`, `CONTRIBUTING.md`, `run.py`

Include in the prompt: (a) the cross-file consistency question — do the three
launchers agree on `PY_VERSION` and `PY_BUILD`, and does the CI hash check's
variant per triple match what each launcher actually downloads; (b) the
constraint that the Mac and Windows launchers must be byte-identical to
`origin/main`; (c) the question of whether the Linux launcher's failure paths
can leave a partly-unpacked `.runtime`. Fix every high-confidence finding before
opening the PR.

- [ ] **Step 7: Rebase and push**

```bash
git fetch origin
git rebase origin/main
git push -u origin feature/linux-support
```

If the rebase reports conflicts, resolve them and re-run Steps 1 and 3 before
pushing — a rebase can silently reintroduce a change to a launcher the gate
covers.

- [ ] **Step 8: Open the PR and stop**

```bash
gh pr create --title "Add Linux support" --body "$(cat <<'BODY'
Adds the third platform: a Linux start file, a Linux branch in the
stopped-app dialog, CI coverage, and docs.

`run.py` needed no behavioural change — it was already platform-neutral.

## What Linux needed that the other two did not

- **A terminal, not a double-click.** Most desktops open a double-clicked
  `.sh` in an editor, and a file-manager-launched script has nowhere to
  print. With no tty the launcher re-execs inside a terminal emulator and,
  failing that, prints the command to run and exits non-zero.
- **A venv probe that cannot lie.** Debian and Ubuntu ship `python3` without
  `python3-venv`, and no import-based check detects it: `venv` is stdlib and
  imports fine, and `import ensurepip` can succeed while its bootstrap
  fails. So the probe builds a throwaway venv for real, on first launch only.
- **The stripped interpreter.** Linux `install_only` is 106 MB against the
  Mac's 24 MB; `install_only_stripped` is the same interpreter without debug
  symbols, at 33 MB. Mac and Windows keep `install_only`.

## Mac and Windows are untouched

`git diff origin/main` over the Mac launcher, the Windows launcher,
`Diagnose (Windows).bat` and `tools/` returns empty. The dialog change is
covered by a new test that runs the JavaScript under node and asserts the
Windows and Mac sentences that `origin/main` produced, so a future edit
that moves them fails CI.

## Verified

Containers against `ubuntu:24.04`, `debian:12-slim` + `python3-minimal`,
`fedora:41` and `alpine:3`, each asserting via `PSC_DIAGNOSE=1` that the
app actually served rather than that a launcher exited 0. Plus the
bad-checksum refusal, a path with spaces, the no-tty refusal, and the Mac
launcher's two paths re-run locally.

Spec: `docs/superpowers/specs/2026-09-08-linux-support-design.md`
Plan: `docs/superpowers/plans/2026-09-08-linux-support.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

**Do not merge.** The PR waits for the user.
