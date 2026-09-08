#!/usr/bin/env python3
"""One-command launcher for the Position Size Calculator (Mac, Windows, Linux).

Creates a private virtual environment beside this file, installs the pinned
dependencies, starts Streamlit on a free port and opens the browser. Safe to
re-run: the install is skipped unless requirements.txt actually changed.

Everything resolves relative to THIS file, so the folder can be moved or
renamed and can sit at a path containing spaces -- which is also why every
subprocess call passes an argument list rather than a shell string.
"""
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

MIN_PYTHON = (3, 10)
# Exclusive ceiling, and it belongs to requirements.txt rather than to this
# file: tradingview-mcp-server==0.8.1 declares Requires-Python >=3.10,<3.14.
# Without the ceiling a 3.14 interpreter passes the check, gets a venv built
# around it, and only then does pip refuse -- so the failure surfaces as an
# unreadable resolver error instead of "that Python is too new". Raise this
# only together with the pin that sets it, and with the two launchers.
MAX_PYTHON = (3, 14)
PREFERRED_PORT = 8765
STARTUP_TIMEOUT_S = 120

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
REQUIREMENTS = HERE / "requirements.txt"
VENV = HERE / ".venv"
# Written only after a clean install, so an install that dies halfway is
# retried on the next run instead of being remembered as done.
STAMP = VENV / ".deps-stamp"

LOG_DIR = HERE / "logs"
ERROR_LOG = LOG_DIR / "run-py-last-error.log"
# Records the app a previous start left running in the background, so starting
# again opens the browser on it rather than standing up a second server.
RUNNING = LOG_DIR / "app-running.json"
# Everything Streamlit said during a start that failed. Written only on failure
# and removed on the next start that works, so its presence means something.
STARTUP_LOG = LOG_DIR / "startup-failure.log"
# Set by the diagnostics: stop once the app has proved it serves, rather than
# blocking on it the way a normal start does.
DIAGNOSE = os.environ.get("PSC_DIAGNOSE") == "1"


def record_failure(summary: str, detail: str = "") -> None:
    """Persist a failure where it can still be read after the window closes.

    On Windows this file is often the only surviving evidence: the console
    disappears with the process, so anything printed to it is lost.
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        parts = [
            f"time         : {datetime.now().isoformat(timespec='seconds')}",
            f"interpreter  : {sys.executable}",
            f"version      : {sys.version.split()[0]}",
            f"platform     : {platform.platform()}",
            f"folder       : {HERE}",
            f"venv present : {venv_python().exists()}",
            "",
            summary,
        ]
        if detail:
            parts += ["", detail]
        ERROR_LOG.write_text("\n".join(parts) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"(could not write {ERROR_LOG}: {exc})", file=sys.stderr)


def fail(message: str, detail: str = "") -> None:
    record_failure(f"ERROR: {message}", detail)
    print(f"\nERROR: {message}\n", file=sys.stderr)
    sys.exit(1)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_step(command: list[str], description: str) -> None:
    """Run one setup step, keeping its output if it fails.

    A failing step -- almost always pip without a working network -- explains
    itself in its own output, and an exit code alone does not. That output used
    to go only to the console, where Windows loses it with the window, so it is
    written to the failure log as well as reprinted here. Nothing is captured
    on success: pip runs --quiet and has nothing to say.
    """
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return
    said = ((result.stdout or "") + (result.stderr or "")).strip()
    if said:
        print(said, file=sys.stderr)
    fail(f"{description} failed (exit code {result.returncode}). "
         f"The app was not started. Details: {ERROR_LOG}",
         "command: " + " ".join(command)
         + (f"\n\noutput:\n{said}" if said else "\n\n(the step printed nothing)"))


def ensure_environment() -> Path:
    python = venv_python()
    if not python.exists():
        print(f"Creating a private Python environment in {VENV.name}/ ...")
        run_step([sys.executable, "-m", "venv", str(VENV)],
                 "Creating the virtual environment")
        if not python.exists():
            fail(f"the environment was created but {python} is missing.")

    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    installed = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    if wanted != installed:
        print("Installing dependencies -- first run only, usually under a minute ...")
        run_step([str(python), "-m", "pip", "install", "--quiet",
                  "--disable-pip-version-check", "--upgrade", "pip"],
                 "Upgrading pip")
        run_step([str(python), "-m", "pip", "install", "--quiet",
                  "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
                 "Installing dependencies")
        STAMP.write_text(wanted, encoding="utf-8")
    return python


def first_free_port(preferred: int) -> int:
    """connect_ex() == 0 means something already answers there, so keep looking.

    Probing rather than hardcoding matters when sharing: a colleague may well
    have something else on 8765, and a port clash makes Streamlit exit with a
    message most people would read as "the app is broken".
    """
    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    fail(f"no free port between {preferred} and {preferred + 19}.")


def serving(port: int) -> bool:
    """Whether Streamlit's own health endpoint answers on this port."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return response.read().strip() == b"ok"
    except OSError:
        return False


def process_alive(pid: int) -> bool:
    """Whether pid still belongs to a running Python.

    os.kill(pid, 0) is the usual POSIX way to ask and is not portable: on
    Windows os.kill terminates the process for every signal it does not
    special-case, so asking there would stop the app instead of asking about
    it. The image name is checked as well because pids are reused -- the
    recorded one may by now belong to something else entirely.
    """
    if os.name == "nt":
        probe = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    else:
        probe = ["ps", "-p", str(pid), "-o", "command="]
    try:
        result = subprocess.run(probe, capture_output=True, text=True)
    except OSError:
        return False
    return "python" in result.stdout.lower()


def already_running() -> int | None:
    """The port of the app an earlier start left running, if it is still up.

    The recorded state is only a hint -- it goes stale when the app is killed or
    the folder is copied. A Streamlit already answering on the preferred port is
    this app, so checking the port directly is what keeps every start on the
    same URL instead of drifting to the next free one.
    """
    try:
        state = json.loads(RUNNING.read_text(encoding="utf-8"))
        pid, port = int(state["pid"]), int(state["port"])
        if process_alive(pid) and serving(port):
            return port
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return PREFERRED_PORT if serving(PREFERRED_PORT) else None


def detached() -> dict:
    """Popen arguments that let the app outlive this launcher and its window.

    A child left in the launcher's process group dies with the terminal that
    started it -- SIGHUP on macOS and Linux, a console control event on
    Windows -- which is precisely what closing the window used to mean.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def startup_detail(capture: Path | None) -> str:
    """Keep everything Streamlit said before it failed, and point the report at it.

    A start that works leaves nothing behind -- main() deletes the scratch file
    the moment the app serves. A start that fails is the opposite case: the
    reason no longer reaches the window, so the whole output is kept in the app
    folder, and its last lines are inlined here so the crash report says
    something without a second file having to be opened.
    """
    if capture is None:
        return "The output above says why."
    try:
        said = capture.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read {capture}: {exc})"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text(said, encoding="utf-8")
        kept = f"Full startup output: {STARTUP_LOG}\n\n"
    except OSError as exc:
        kept = f"(could not write {STARTUP_LOG}: {exc})\n\n"
    return kept + "Streamlit's last output:\n" + "\n".join(said.splitlines()[-40:])


def wait_until_serving(process: subprocess.Popen, port: int,
                       capture: Path | None = None) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail(f"Streamlit exited during startup (code {process.returncode}). "
                 f"Details: {ERROR_LOG}", startup_detail(capture))
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    process.terminate()
    fail(f"Streamlit did not start within {STARTUP_TIMEOUT_S}s. "
         f"Details: {ERROR_LOG}", startup_detail(capture))


def main() -> None:
    supported = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} to {MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1}"
    if sys.version_info < MIN_PYTHON:
        fail(f"Python {supported} is required, but this is "
             f"{sys.version.split()[0]}. Install a newer Python from "
             "https://www.python.org/downloads/")
    if sys.version_info >= MAX_PYTHON:
        # Separated from the too-old case because the remedy is the opposite:
        # there is nothing to install, and the start file already ships a
        # working interpreter for exactly this situation.
        fail(f"Python {supported} is required, but this is "
             f"{sys.version.split()[0]}, which the app's pinned dependencies "
             "do not support yet. Start the app with the start file for your "
             "system instead of running run.py directly -- it will fetch a "
             "private copy of a supported Python.")
    for required in (APP, REQUIREMENTS):
        if not required.exists():
            fail(f"{required.name} is missing from {HERE}. Keep every file from "
                 "the shared folder together.")

    # The app now outlives the window that started it, so a second start would
    # otherwise stand up a second server on the next free port and leave the
    # first one running unseen. DIAGNOSE skips the shortcut: its whole job is to
    # prove a start works, which a browser tab pointed at an older one does not.
    if not DIAGNOSE:
        running = already_running()
        if running is not None:
            url = f"http://localhost:{running}"
            print(f"\n  Position Size Calculator is already running at {url}")
            print("  Opening it in your browser.\n")
            webbrowser.open(url)
            return

    python = ensure_environment()
    port = first_free_port(PREFERRED_PORT)
    url = f"http://localhost:{port}"
    command = [
        str(python), "-m", "streamlit", "run", str(APP),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]

    if DIAGNOSE:
        # The debug path keeps every line on the console, because that console
        # is what tools/diagnose.ps1 captures into its report.
        process = subprocess.Popen(command)
        wait_until_serving(process, port)
        print(f"\n  Position Size Calculator is running at {url}")
        print("  Diagnostics: startup succeeded, stopping the app again.\n")
        process.terminate()
        process.wait()
        return

    # Detaching and redirecting are two halves of one change, and neither works
    # without the other. Detached, the app survives this window closing -- but a
    # child still writing to that window's console writes into a vanished
    # terminal once it does. Redirecting is also what empties the window, which
    # used to scroll a Streamlit banner, a Uvicorn line and a screenful of
    # library deprecation warnings past the one line saying where the app is.
    #
    # Nothing is written to the app's folder for it. The scratch file below is
    # in the system temp directory and is deleted the moment the app serves;
    # until then it is the only thing that can say why a start failed, since
    # that reason no longer reaches the window either.
    capture = Path(tempfile.gettempdir()) / f"psc-startup-{os.getpid()}.log"
    with capture.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=sink, stderr=subprocess.STDOUT,
                                   **detached())
        try:
            wait_until_serving(process, port, capture)
        except KeyboardInterrupt:
            print("\nStopping ...")
            process.terminate()
            process.wait()
            return
    # An earlier failure's logs would otherwise sit in the app folder looking
    # like a current problem -- and tools/diagnose.ps1 reads the error log back
    # into its report, so a stale one shows up as exceptions from a start that
    # actually worked. The app is already serving by this point, so a cleanup
    # that cannot proceed is worth a note and nothing more.
    for stale in (STARTUP_LOG, ERROR_LOG):
        try:
            stale.unlink(missing_ok=True)
        except OSError as exc:
            print(f"  (could not remove the old {stale.name}: {exc})",
                  file=sys.stderr)
    try:
        capture.unlink()
    except OSError:
        # Expected on Windows, which refuses to delete a file its writer still
        # holds open. It is in the system temp directory rather than the app
        # folder, so the OS clears it out in its own time.
        pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        RUNNING.write_text(json.dumps({"pid": process.pid, "port": port}),
                           encoding="utf-8")
    except OSError as exc:
        # Not fatal: the app is up. The cost is that the next start cannot tell
        # it is, and starts a second one, so say so rather than swallow it.
        print(f"  (could not record the running app in {RUNNING}: {exc})",
              file=sys.stderr)

    print(f"\n  Position Size Calculator is running at {url}")
    print("  It keeps running in the background -- this window can close now.")
    print("  Your positions are saved in this folder, in positions.json.\n")
    webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Without this the traceback goes only to a console that is about to
        # disappear, which is exactly the failure being debugged.
        detail = traceback.format_exc()
        record_failure("Unhandled exception -- the app did not start.", detail)
        print(detail, file=sys.stderr)
        print(f"\nERROR: the app crashed. Details: {ERROR_LOG}\n", file=sys.stderr)
        sys.exit(1)
