# Contributing

This is a small personal tool, kept deliberately simple: one Streamlit file,
a launcher, and pinned dependencies. Issues and pull requests are welcome.

## Running the tests

No test framework to install — the tests are plain scripts that run against
the venv the launcher already built.

```sh
python3 tests/test_state.py     # state, validation, sizing maths. No network.
python3 tests/test_lookup.py    # symbol search and refresh. Needs network.
```

`test_state.py` is what CI runs. `test_lookup.py` talks to TradingView, so it
is excluded from CI — a failure there is as likely to mean the API was slow as
it is to mean the code broke, and a suite that cries wolf gets ignored. Run it
by hand when you touch search or refresh.

## How the tests reach the code

`app.py` is a Streamlit script, not an importable module — importing it top to
bottom would execute page-render calls and need a live Streamlit runtime. So
`tests/_lift.py` parses it and executes only the definitions above the first
render call. The tests therefore exercise the shipping functions directly;
there is no second copy of the logic to drift out of sync.

`DATA_FILE` is derived inside `app.py` as `Path(__file__).parent /
"positions.json"`. The harness substitutes `__file__` with a path inside a
temporary directory, so a test cannot reach a real portfolio file. `lift_app()`
asserts this and refuses to run if it ever stops holding.

If you move or rename `run_pending_symbol_check`, update `_CUTOFF_FUNC` in
`tests/_lift.py` — that function name is the boundary between "logic" and
"page rendering".

## House style

- **Comments explain *why*, not *what*.** Several CSS rules in `app.py` look
  arbitrary and are not: they encode measured browser behaviour, such as the
  width below which Streamlit removes a number input's `-`/`+` buttons from the
  DOM entirely. Those comments carry the measurements. Please keep them
  accurate if you change the values, and re-measure rather than guess.
- **Fail loudly.** Don't fall back to a default when the operation was supposed
  to produce real data — a wrong position size is worse than a visible error.
- **The launchers must keep working on macOS, Windows and Linux.** Everything in
  `run.py` resolves relative to the file itself and passes argument lists
  rather than shell strings, so the folder can be moved, renamed, or sit at a
  path containing spaces. Keep it that way.

## Dependencies

`requirements.txt` is pinned exactly, `streamlit` most of all: the CSS in
`app.py` targets specific Streamlit DOM structure, so a version bump needs the
layout re-checked at narrow widths, not just a green test run.
