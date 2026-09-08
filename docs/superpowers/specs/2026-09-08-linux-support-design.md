# Linux support — design

Date: 2026-09-08
Status: approved for implementation

## Problem

The app targets macOS and Windows. Each has a double-click start file whose
only job is to hand a Python 3.10–3.13 to `run.py`; `run.py` then builds a
private `.venv`, installs the pinned requirements, starts Streamlit and opens
a browser. A Linux user today has no start file, and `app.py`'s stopped-app
dialog tells them to double-click two files that do not exist in their folder.

`run.py` itself is already platform-neutral: `venv_python()` branches on
`os.name`, and `_atomic_write()` guards its `fchmod` call with
`hasattr(os, "fchmod")`. So Linux support is a launcher, docs and CI problem,
plus one shared block of JavaScript in `app.py`.

## Decisions

Four decisions were made before this spec, and the design follows them.

### 1. Terminal-first, with a graceful double-click

Linux does not reliably run a double-clicked `.sh`: GNOME Files opens
executable text files in an editor unless the user changes a preference, and a
script started from a file manager gets no terminal, so error messages and the
"press Return to close" pause have nowhere to go. That is the same class of
failure PR #4 and PR #5 were about.

So the README documents running the script from a terminal as *the* Linux path,
and the script additionally detects that it has no tty and re-execs itself
inside a terminal emulator. Where no emulator or no display exists it prints
the exact command to run and exits non-zero. It never fails silently.

### 2. Bundled Python fallback, gated on a real venv test

Linux keeps the promise the README makes to every user ("You do not need to
install Python, or anything else, first"). Two architectures are pinned, from
the same CPython 3.12.14 / build 20260901 the other two launchers already use.

Linux adds a failure mode neither existing platform has: on Debian and Ubuntu,
`python3` is present but `python3-venv` is often not, so a version probe passes
and `python3 -m venv` then fails. **No import-based probe detects this
reliably.** `venv` is stdlib and imports fine; what Debian strips is
`ensurepip`'s bundled wheels, and the failure happens inside `venv`'s
pip-bootstrap step — `import ensurepip` can itself succeed while
`ensurepip.bootstrap()` fails.

The only trustworthy probe is doing the real thing: `python3 -m venv` into a
temporary directory. It runs **only when `.venv` is absent** — i.e. first
launch — because once `.venv` exists its existence is the answer. The
downloaded runtime skips the probe; it always has working `ensurepip`.

### 3. A separate, self-contained launcher

`Start Calculator (Linux).sh` stands alone, like the other two, rather than
sharing a sourced provisioning library with the Mac launcher. This keeps every
launcher a single file that works from a partly-extracted folder, and leaves
the working, CI-verified Mac launcher at zero bytes changed.

The cost is roughly 35 duplicated lines of download/verify/unpack logic. That
is paid for with a check rather than a refactor: CI asserts all three launchers
pin the same CPython version and build, and verifies every pinned SHA-256
against the published release.

### 4. Linux downloads the stripped build

Measured against release 20260901 (method validated against the Mac asset,
which came back at 24.0 MB and matches the README's documented 24 MB):

| Asset | `install_only` | `install_only_stripped` |
|---|---|---|
| `aarch64-apple-darwin` | 24.0 MB (in use) | not used |
| `x86_64-pc-windows-msvc` | 44.0 MB (in use) | not used |
| `x86_64-unknown-linux-gnu` | 106.2 MB | **32.6 MB** |
| `aarch64-unknown-linux-gnu` | 79.7 MB | **27.8 MB** |

The Linux `install_only` tarballs are over four times the Mac download.
`install_only_stripped` is the same interpreter with debug symbols removed —
irrelevant to running a Streamlit app — and brings Linux in line with the other
platforms. Linux therefore pins the stripped variant.

Mac and Windows keep `install_only` and are not touched. The CI hash check must
consequently know the variant per platform, not assume one asset name.

## Non-goals

- **No Linux diagnostics tool.** `Diagnose (Windows).bat` and
  `tools/diagnose.ps1` exist because a Windows console vanishes with its
  process, taking the evidence with it. A Linux terminal does not, and macOS
  ships no diagnostics either. Parity here is with Mac.
- **No `.desktop` file**, no `.deb`/`.rpm`/AppImage/Flatpak packaging.
- **No bundled Python for musl/Alpine.** Detected and reported with a targeted
  message instead of downloading a glibc binary that cannot execute.
- **No architectures beyond x86_64 and aarch64.** Anything else gets a clear
  message pointing at the distro's own Python.
- **No WSL-specific handling.** WSL is treated as ordinary Linux. Browser
  opening there depends on `wslview` or similar being present; accepted.

## Design

### New: `Start Calculator (Linux).sh`

Committed mode 755. GitHub's Download-ZIP preserves the executable bit —
verified: the Mac `.command` arrives as mode `100755` in the zipball — but some
extractors drop it, so the README carries a `chmod +x` fallback.

Same contract as the other two launchers: obtain a Python 3.10–3.13 that can
build a venv, then exec `run.py`. Adapted from `Start Calculator (Mac).command`,
keeping its two load-bearing rules:

- a runtime downloaded on an earlier run wins over the system Python, because
  `run.py`'s `.venv` is bound to whichever interpreter created it;
- the exclusive 3.14 ceiling, because `tradingview-mcp-server==0.8.1` declares
  `Requires-Python >=3.10,<3.14`. A too-new interpreter must fail the probe and
  fall through to the download rather than reach pip and fail there.

Linux-specific behaviour:

**No-tty re-exec.** If neither stdin nor stdout is a tty and `$DISPLAY` or
`$WAYLAND_DISPLAY` is set, re-exec inside the first emulator found:
`x-terminal-emulator`, `gnome-terminal`, `konsole`, `xfce4-terminal`, `kitty`,
`alacritty`, `foot`, `xterm`. With no emulator or no display, print the command
to run and exit 1.

`PSC_NO_REEXEC=1` skips the whole check and runs directly. It does double duty,
because both callers mean the same thing operationally — "do not go looking for
a terminal":

- the script sets it before re-execing, which is what stops a re-exec loop;
- **CI and container tests must set it.** Every launcher step runs as
  `./launcher < /dev/null | tee out.txt`, so stdin is redirected and stdout is
  piped — neither is a tty — and a runner has no `DISPLAY`. Without this
  variable, every normal CI launcher step on Linux would take the
  no-display branch and test the refusal path instead of the launcher. The
  dedicated no-tty step is precisely the one that leaves it unset.

**Usability probe.** Version check as on Mac, plus — only when `.venv` is
absent, and only for a *system* `python3` — a real `python3 -m venv` into a
`mktemp -d` that is removed immediately afterwards. Costs a few seconds on
first launch only.

**Tool fallbacks the Mac launcher does not need.** Checksum: `sha256sum`, then
`shasum -a 256`, then `openssl dgst -sha256`. Download: `curl -fL`, then
`wget -O`. Minimal images commonly ship only one of each.

**musl pre-check.** `ldd --version` reporting musl produces a targeted "install
python3 from your package manager" message *before* a 33 MB download that
could not execute.

**Architecture map.** `x86_64` and `aarch64`/`arm64` to the two pinned triples;
anything else to the clear-message path.

Pinned values (verified against the release's `SHA256SUMS`):

| Triple | SHA-256 of `install_only_stripped` |
|---|---|
| `x86_64-unknown-linux-gnu` | `72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c` |
| `aarch64-unknown-linux-gnu` | `577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d` |

### Changed: `app.py` — stopped-app dialog

The dialog's platform logic (around lines 2381–2478) currently reads
`IS_WIN ? [WIN_FILE] : IS_MAC ? [MAC_FILE] : [both]`, and picks its instruction
sentence from `FILES.length > 1`.

That length test is the trap. Today "one file" implies "a recognised platform"
and "two files" implies "unrecognised", so the sentence can be derived from the
count. Adding Linux breaks that inference: a Linux user has one file but needs
a different sentence, because we are not promising double-click on Linux. The
sentence therefore becomes explicit per platform instead of derived.

| Platform | Sentence | Named thing |
|---|---|---|
| Windows | "To start it again, double-click this file in File Explorer:" | `Start Calculator (Windows).bat` |
| Mac | "To start it again, double-click this file in Finder:" | `Start Calculator (Mac).command` |
| Linux | "To start it again, open a terminal in this folder and run:" | `./Start Calculator (Linux).sh` |
| Unrecognised | existing multi-file fallback wording, extended to all three | all three files |

`FILE_BROWSER` gains `"your file manager"` for Linux, which is what Linux
desktops call it. The existing `"your file browser"` fallback for unrecognised
platforms is left alone.

Windows and Mac output must be byte-identical before and after this edit; see
Non-regression below.

### Investigated, not pre-emptively changed: table height

`app.py` around line 3966 already records this risk in its own words: the
AgGrid pixel height adds no buffer for a horizontal scroll bar because macOS
overlay scroll bars take no layout height, and it is "UNVERIFIED on
Windows/Linux classic scrollbars, where ag-Grid gives it a real flex slot and
the last row may clip."

This gets tested in a Linux browser, not patched on suspicion. If it does not
reproduce, nothing changes and the finding is reported.

If it does reproduce, the fix must **measure at runtime**, never pad the
constants: `GRID_HEADER_HEIGHT` and `GRID_ROW_HEIGHT` are shared by all three
platforms, and the same comment records that padding them recreates a
blank-strip bug. A runtime measurement is a no-op where the scroll bar takes no
layout height, which is macOS.

### Changed: `run.py`

Docstring only: `"(macOS + Windows)"` becomes `"(macOS, Windows and Linux)"`.

No behavioural change is needed. Its venv-creation failure already surfaces
Python's own message, which on Debian names the exact package to install, so
there is nothing to improve there.

### Changed: `.github/workflows/ci.yml`

Additive. Every existing step is already guarded by `if: runner.os == ...`, so
the macOS and Windows jobs keep running verbatim.

- `test` job matrix gains `ubuntu-latest` (against Python 3.10 and 3.12).
- `launcher` job matrix gains `ubuntu-latest`, with two steps mirroring the
  macOS pair: uses-the-machine's-Python (asserts no `.runtime` is created), and
  downloads-its-own via a PATH shim hiding `python3` (asserts `.runtime` and
  the exact `3.12.14`). Both set `PSC_NO_REEXEC=1`, for the reason given in the
  launcher section.
- The hash-verification step gains both Linux triples and, because Linux pins a
  different variant, becomes variant-aware per platform. It also gains an
  assertion that all three launchers pin the same version and build — this is
  the drift guard that replaces a shared library.
- **New `launcher-linux-novenv` job**, `container: debian:12-slim`, with
  `python3-minimal` installed and `python3-venv` deliberately absent. This is
  the only job that can prove the venv fallthrough fires: the launcher must
  reject the system `python3` and end up on the downloaded runtime.
- **New no-tty step:** run the launcher with stdin and stdout redirected,
  `DISPLAY` unset and `PSC_NO_REEXEC` **unset**; assert the "open a terminal"
  instruction is printed and the exit code is non-zero.

### Changed: `README.md`

Linux content is added to the lists that are already "find your OS and do that
one thing" (the install walkthrough, `Installing Python`) and kept *out* of the
shared troubleshooting table, which gets its own Linux section instead.

**Additive, following the existing per-OS bullet pattern:**

- Step 2 (Unzip): a Linux bullet (`unzip`, or double-click the ZIP).
- Step 3 (Start it): a Linux bullet giving the terminal command
  `./"Start Calculator (Linux).sh"`, and noting double-click works on some
  desktops but opens an editor on many.
- `Installing Python`: a Linux block with `apt` / `dnf` / `pacman`, calling out
  that `python3-venv` matters as much as `python3` on Debian and Ubuntu.
- `Advanced: install with Git`: the Mac command is already correct for Linux, so
  its heading becomes "on **Mac** or **Linux**" — a one-word edit, no new block.

**New section `### Linux: if it does not start`,** placed after
`### Windows: getting a diagnosis` so nothing above it shifts and the shared
table's "see **Windows: getting a diagnosis** below" still points at the very
next section. Four rows, unprefixed because the heading scopes them:
double-click opens a text editor; `Permission denied` (the `chmod +x` fallback,
needed because some extractors drop the exec bit); it downloads its own Python
even though `python3` is installed (expected on Debian/Ubuntu, not a fault);
and "not one we have a Python download for" (non-x86-64/ARM64, or musl).

**The shared `### If it does not start` table changes zero bytes.** Its two
generic rows already read correctly on Linux, and leaving it untouched turns
requirement 1 below into a diff of nothing rather than a diff to be argued
harmless. This is a deliberate departure from the table's existing habit of
OS-prefixing rows (`Mac:`, `Windows:`): three more prefixed rows would make the
first thing a struggling user reads mostly about other people's platforms.

**Three sentences must change** because they are false on Linux as written: the
intro's "Download, unzip, double-click" promise (gains a parenthetical pointing
at step 3), step 3's "and double-click:" lead-in (becomes "and start the file
for your system:"), and the private-Python size list (gains "33 MB on Linux";
arm64's 28 MB is not worth a parenthetical here). Linux is also added to the
sentence naming the platforms CI covers.

## Non-regression on macOS and Windows

The user's explicit requirement. Three checks, one of which constrains the
order of implementation.

1. **Baseline capture before any `app.py` edit.** Drive the *current* app in a
   browser with the platform and user-agent spoofed to Windows and to Mac, and
   record the stopped-app dialog's rendered instruction sentence and filename
   box. After the Linux branch lands, the same two spoofed runs must produce
   byte-identical output. A baseline taken afterwards would prove nothing,
   so this step precedes the edit.
2. **Pre-PR gate:** this must return empty —
   `git diff origin/main -- "Start Calculator (Mac).command" "Start Calculator (Windows).bat" "Diagnose (Windows).bat" tools/`
3. **Existing CI is the alarm.** The `launcher` job's four macOS and Windows
   steps and the whole `diagnostics` job are unmodified. Additionally, re-run
   the Mac launcher locally on both paths (system Python, and the PATH-shim
   download path).

## Verification

Docker is available locally, so the launcher is tested against real distros
rather than only in CI. `run.py` already has a `PSC_DIAGNOSE=1` mode that
starts Streamlit, waits until it genuinely serves, then stops — so each
container asserts the app **served**, not merely that a launcher exited 0.

| Case | Environment | Expected |
|---|---|---|
| No Python at all | `ubuntu:24.04` | downloads the stripped runtime, app serves |
| Python without venv | `debian:12-slim` + `python3-minimal` | rejects system Python, downloads runtime, app serves |
| Python with venv | `fedora:41` | uses system Python, no `.runtime` created, app serves |
| Second run | any of the above, re-run | reuses `.venv`, skips the venv probe, app serves |
| No tty, no display | `ubuntu:24.04`, `DISPLAY` and `PSC_NO_REEXEC` unset | prints the terminal instruction, exits non-zero |
| Bad checksum | pinned hash temporarily altered | refuses the download, does not install it |
| musl | `alpine:3` | targeted message, no download attempted |
| Folder path with spaces | any | works (every call passes an argument list) |

Browser-level checks in a Linux container: the stopped-app dialog's Linux
wording and filename, and the table-height scroll bar question above.

Unit tests `tests/test_state.py` continue to run in CI, now on Linux too.
`tests/test_lookup.py` stays out of CI — it calls TradingView, and a red build
would as often mean "the API was slow" as "the code broke".

## Out of scope for this change

The accessibility gap PR #6 deferred — the consolidated help icon is a
`tabindex="-1"` span that opens on hover only, with no keyboard or
screen-reader path — remains open and is unrelated to Linux support.
