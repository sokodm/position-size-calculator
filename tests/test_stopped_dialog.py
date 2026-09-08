"""The stopped-app dialog's platform decision, run as real JavaScript.

The dialog is JavaScript embedded in a Python string, so nothing else in the
suite can reach it. This extracts the pure decision block -- everything from
the navigator sniff to the instruction sentence, which touches no DOM -- and
runs it under node against fake navigators.

The Windows and Mac expectations are the values origin/main produced before
Linux support was added. They are asserted rather than described: the point of
this file is that adding a third platform did not move the other two.
"""

import ast
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


SPLICE = "\x00SPLICE\x00"


def _pieces(node):
    """The operands of a string concatenation, in source order.

    ast.walk is breadth-first and would reorder a + b + c, so the tree is
    flattened left-to-right by hand instead.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pieces(node.left) + _pieces(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [SPLICE]


def decision_block():
    """The marked region as the browser receives it.

    Sliced out of app.py as raw text, the Windows path's backslash escape
    arrives at node doubled -- one more backslash than a browser ever sees --
    because Python's own escapes have not been resolved yet. So parse app.py
    and evaluate the string literal rather than reading its source text.
    """
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", ""))
        if name != "html":
            continue
        js = "".join(_pieces(node.args[0]))
        if START not in js or END not in js:
            continue
        block = js[js.index(START) + len(START):js.index(END)]
        if block.count(SPLICE) != 1:
            raise AssertionError(
                "expected exactly one Python splice (APP_DIR_NAME) inside the "
                "marked block, found %d" % block.count(SPLICE)
            )
        return block.replace(SPLICE, json.dumps(FOLDER))
    raise AssertionError(
        "no components.html(...) call carries the extraction markers:\n"
        "  " + START + "\n  " + END
    )


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
