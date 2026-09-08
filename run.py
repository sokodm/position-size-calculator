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
import os
import platform
import socket
import subprocess
import sys
import time
import traceback
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
    result = subprocess.run(command)
    if result.returncode != 0:
        fail(f"{description} failed (exit code {result.returncode}). "
             "The app was not started.",
             "command: " + " ".join(command))


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


def wait_until_serving(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail(f"Streamlit exited during startup (code {process.returncode}). "
                 "The output above says why.")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    process.terminate()
    fail(f"Streamlit did not start within {STARTUP_TIMEOUT_S}s.")


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

    python = ensure_environment()
    port = first_free_port(PREFERRED_PORT)
    url = f"http://localhost:{port}"

    process = subprocess.Popen([
        str(python), "-m", "streamlit", "run", str(APP),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ])
    try:
        wait_until_serving(process, port)
        print(f"\n  Position Size Calculator is running at {url}")
        if DIAGNOSE:
            print("  Diagnostics: startup succeeded, stopping the app again.\n")
            process.terminate()
            process.wait()
            return
        print("  Your positions are saved in this folder, in positions.json.")
        print("  Leave this window open while you use the app; close it to stop.\n")
        webbrowser.open(url)
        process.wait()
    except KeyboardInterrupt:
        print("\nStopping ...")
        process.terminate()
        process.wait()


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
