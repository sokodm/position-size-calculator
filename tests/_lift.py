"""Loads the real functions out of app.py so tests exercise shipping code.

app.py is a Streamlit script, not an importable module: importing it top to
bottom would execute page-render statements and need a live Streamlit runtime.
So the tests parse it and execute only the definitions above the first render
call, which is everything the calculations and persistence live in.

The payoff is that these tests cannot drift from what ships -- there is no
second copy of the logic to keep in sync.

Safety: DATA_FILE is derived inside app.py as `Path(__file__).parent /
"positions.json"`. Substituting __file__ with a path inside a temp directory
redirects persistence there, so a test physically cannot open the real
portfolio. lift_app() asserts this before returning and raises if it ever
stops being true.
"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app.py"

# Definitions below this function are page-render code that needs a Streamlit
# runtime. Anything the tests need from further down must be lifted explicitly
# with lift_extra().
_CUTOFF_FUNC = "run_pending_symbol_check"


def _parsed():
    # Explicit encoding, not the locale default: app.py holds non-ASCII (the
    # multiplication sign and em dashes in its comments and UI strings), and on
    # Windows read_text() defaults to cp1252, which cannot decode them. Without
    # this the harness dies before a single test runs.
    source = APP.read_text(encoding="utf-8")
    return source, ast.parse(source)


def lift_app(data_dir):
    """Execute app.py's definitions with persistence redirected to data_dir."""
    data_dir = Path(data_dir)
    source, tree = _parsed()
    cutoff = next(n.end_lineno for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == _CUTOFF_FUNC)

    ns = {"__file__": str(data_dir / "app_lifted.py"), "__name__": "app_lifted"}
    for node in tree.body:
        if node.end_lineno > cutoff:
            break
        src = ast.get_source_segment(source, node) or ""
        # Top-level `st.something(...)` calls render UI; skip them. Definitions
        # and constants are what the tests are after.
        if isinstance(node, ast.Expr) and src.lstrip().startswith("st."):
            continue
        exec(compile(ast.Module([node], []), str(APP), "exec"), ns)

    resolved = Path(ns["DATA_FILE"]).resolve()
    if data_dir.resolve() not in resolved.parents:
        raise AssertionError(
            f"REFUSING TO RUN: DATA_FILE escaped the sandbox -> {resolved}. "
            "app.py's DATA_FILE no longer derives from __file__; fix the test "
            "harness before running anything that writes."
        )
    return ns


def lift_extra(ns, *names):
    """Lift named definitions from below the cutoff into an existing namespace."""
    source, tree = _parsed()
    for name in names:
        node = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        if node is None:
            raise AssertionError(f"{name}() not found in app.py -- was it renamed?")
        exec(compile(ast.Module([node], []), str(APP), "exec"), ns)
    return ns


class Checker:
    """Minimal pass/fail reporter so the tests need no pytest install."""

    def __init__(self):
        self.failures = 0

    def __call__(self, label, ok, detail=""):
        ok = bool(ok)
        self.failures += (not ok)
        suffix = f" -- {detail}" if detail else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
        return ok

    def equals(self, label, got, want):
        return self(label, got == want, "" if got == want else f"got {got!r}, want {want!r}")

    def report(self):
        print(f"\n{'ALL PASS' if not self.failures else f'{self.failures} FAILURE(S)'}")
        return 1 if self.failures else 0
