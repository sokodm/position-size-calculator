"""Position size calculator — recreates the "Position Size Calculator ($)"
tab of Trading_Course_Sheet_2_0.xlsx as a live multi-asset tool.

ATR is pulled by importing the tradingview_mcp package directly (the same
library that powers the tradingview MCP server) rather than going through
Claude/MCP at runtime. That means running this app costs zero Claude tokens
and needs no MCP/Claude session at all — it's a standalone Python/Streamlit
app that talks straight to TradingView's data feed.

Run with:  ./.venv/bin/streamlit run app.py
"""
import html
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, JsCode

# TradingView's CloudFront CDN returns HTTP 429 (empty body) specifically for the
# "tradingview_ta/{version}" User-Agent that tradingview_ta hardcodes into every TA
# request (confirmed via side-by-side testing: identical payload with a generic UA
# gets HTTP 200; the same payload with tradingview_ta's UA gets HTTP 429). That's the
# real cause of analyze_coin() failures/slow retries below, not request volume or an
# upstream outage. tradingview_ta exposes no way to override its headers, so rewrite
# just that one header on the way out — same endpoint, same request volume, and every
# other requests.post call in this app (none of which set that header) is untouched.
_ORIG_REQUESTS_POST = requests.post


def _patched_requests_post(url, *args, **kwargs):
    headers = kwargs.get("headers")
    if headers and str(headers.get("User-Agent", "")).startswith("tradingview_ta/"):
        headers = dict(headers)
        headers["User-Agent"] = "Mozilla/5.0"
        kwargs["headers"] = headers
    return _ORIG_REQUESTS_POST(url, *args, **kwargs)


requests.post = _patched_requests_post

# tradingview_mcp's screener_provider throttles its TA calls (TRADINGVIEW_MCP_MAX_INFLIGHT
# concurrent calls, TRADINGVIEW_MCP_MIN_INTERVAL_S minimum gap between call starts) as a
# defensive measure against the CloudFront 429 above. Measured once, by hand:
# with the old defaults (max_inflight=2, min_interval=0.5s) a 3-asset-class "All" search
# (~20 TA calls) took ~10s wall-clock, and (calls-1)*0.5s alone accounted for nearly all
# of it -- the actual per-call network time is under a second now that the 429 is fixed.
# Now that the UA rewrite addresses the real cause directly (and 30 concurrent scanner
# requests were confirmed to succeed with no rate-limiting), the pacing gate is no longer
# buying us safety, only latency. Raise the cap to match _HTTP_POOL below and shrink the
# gap to a token amount (cuts the same search to ~2s); must be set before importing
# screener_provider since its semaphore size is fixed at import time.
os.environ.setdefault("TRADINGVIEW_MCP_MAX_INFLIGHT", "8")
os.environ.setdefault("TRADINGVIEW_MCP_MIN_INTERVAL_S", "0.05")

from tradingview_mcp.core.services.screener_service import analyze_coin  # noqa: E402

APP_DIR = Path(__file__).parent
DATA_FILE = APP_DIR / "positions.json"

# Shown in the stopped-app dialog as the folder holding the start files, so the
# reader gets a path they can actually follow. It has to be derived rather than
# written down: a GitHub "Download ZIP" unpacks to position-size-calculator-main,
# a clone gives position-size-calculator, and a renamed folder gives something
# else again -- any literal here would be wrong for most readers. This is the
# folder the running app is in, which is by definition the right one, and it
# carries no absolute path and no user name.
APP_DIR_NAME = APP_DIR.name

# ag-Grid is TOLD these heights (headerHeight/rowHeight in configure_grid_options),
# so any AgGrid() call that also sizes its iframe by row count must derive that
# height from these same two constants -- HEADER + ROW*n, nothing else. A literal
# on either side drifts, leaving the iframe too short (clips the last row) or too
# tall (a blank strip below the last row that reads as a stray empty box).
GRID_HEADER_HEIGHT = 34
GRID_ROW_HEIGHT = 42
# What ag-Grid's own chrome takes on top of HEADER + ROW*n for a grid whose
# iframe height is locked and whose horizontal scroll widget is in the layout.
# Measured on Linux (Chromium 152, classic scrollbars) at both 2 and 3 rows: the
# header renders 35 rather than the 34 it is told, plus a 1px gap above and below
# that widget. Constant across row counts -- that is why it is its own term and
# not padding on the two above, which multiply and would recreate the blank-strip
# bug. Without it the body viewport is 3px short of its content and every table
# carries a permanent vertical scroll bar. Only the positions grid is measured
# against this; a grid that renders no horizontal scroll widget has not been
# checked and should not assume the same 3px.
GRID_CHROME_HEIGHT = 3

SHOW_LAST_REFRESH_CHANGES = False

ASSET_CLASSES = ["Crypto", "Stock", "Commodity"]

# The ATR multiple's floor and increment, shared by the add-form widget, the
# grid editor's validator and max_safe_atr_multiple(). The floor must stay a
# whole number of steps: HTML anchors a number input's valid values at
# min + n*step, so a floor off that grid marks every value the "%.1f" display
# can show as a stepMismatch (min=0.01/step=0.1 made even the 1.5 default
# announce as aria-invalid to screen readers).
ATR_MULTIPLE_MIN = 0.1
ATR_MULTIPLE_STEP = 0.1

# Biggest/most-liquid venues first for each asset class. resolve_exchange_and_fetch()
# tries the top 3 live, then up to 5 more from this list, until one actually lists
# the symbol — no manual exchange entry needed.
EXCHANGE_PRIORITY = {
    "Crypto": ["BINANCE", "COINBASE", "BYBIT", "OKX", "KUCOIN", "GATEIO", "MEXC", "BITGET", "BITFINEX", "HUOBI"],
    "Stock": ["NASDAQ", "NYSE", "AMEX", "HKEX", "ASX", "SSE", "SZSE", "TWSE", "TPEX", "BIST", "BURSA", "EGX", "TADAWUL"],
    "Commodity": ["TVC", "CAPITALCOM"],
}


def exchange_candidates(asset_class: str) -> list[str]:
    """The exchanges actually tried for one symbol fetch (top 8 by priority).
    Shared by resolve_exchange_and_fetch() (which fetches them) and
    refresh_all_positions() (which clears their cache entries) so the two
    sets can never drift apart and leave a stale entry the fetch still reads."""
    return EXCHANGE_PRIORITY.get(asset_class, EXCHANGE_PRIORITY["Crypto"])[:8]

# If the user types a bare ticker ("HYPE") with no quote currency, Crypto lookup
# defaults to USD ("HYPEUSD") rather than trying every quote currency in turn.
# Typing the pair explicitly ("HYPEUSDT", "ETHBTC") always overrides this default.
CRYPTO_QUOTE_CURRENCIES = ["USDT", "USDC", "BUSD", "FDUSD", "USD", "BTC", "ETH", "EUR", "GBP", "TRY"]


def with_default_quote(symbol: str) -> str:
    # CRYPTO_QUOTE_CURRENCIES includes "BTC" and "ETH" themselves (for pairs like
    # ETHBTC), so a bare "BTC" or "ETH" trivially satisfies endswith(quote) against
    # itself -- len() guards against treating the whole symbol as its own quote.
    if any(len(symbol) > len(q) and symbol.endswith(q) for q in CRYPTO_QUOTE_CURRENCIES):
        return symbol
    return symbol + "USD"


def format_symbol_display(symbol: str, asset_class: str) -> str:
    """Insert a base/quote separator ("BTCUSDT" -> "BTC/USDT") for pairs. Stock
    tickers are plain company codes, not base/quote pairs, so left untouched.
    Quote suffixes overlap (anything ending in BUSD also ends in USD), so a
    symbol can split more than one way: BNBUSD must read BNB/USD, not BN/BUSD.
    Prefer the first split (in quote-priority order) whose base is a plausible
    ticker (3+ chars) -- ETHBUSD still reads ETH/BUSD because its 3-char base
    is found before the USD split -- falling back to the first split at all
    for genuine short bases like OP/USD."""
    if asset_class == "Stock":
        return symbol
    splits = [
        (symbol[:-len(q)], q)
        for q in CRYPTO_QUOTE_CURRENCIES
        if len(symbol) > len(q) and symbol.endswith(q)
    ]
    for base, q in splits:
        if len(base) >= 3:
            return f"{base}/{q}"
    if splits:
        return f"{splits[0][0]}/{splits[0][1]}"
    return symbol

def _fmt_money(value: float) -> str:
    """Dollar display for the Portfolio Size field. Cents are shown only when
    present: always formatting with ",.0f" made the field display 1,235 while
    1234.56 was what got saved and computed with -- the display must never
    disagree with the persisted value."""
    return f"{value:,.2f}" if value % 1 else f"{value:,.0f}"


def _money_field(value: float) -> str:
    """Portfolio Size box contents, where 0 is the never-entered sentinel.

    Renders empty rather than "0" so a fresh install shows a blank field to fill
    in, instead of a plausible-looking $0 that silently sizes every position to
    nothing.
    """
    return _fmt_money(value) if value > 0 else ""


def _finite_number(value) -> bool:
    """True only for a real, usable number arriving from outside this app.

    NaN is the case that matters: every numeric guard in this file is a
    comparison, and *all* NaN comparisons are False, so a NaN never fails a
    check -- it passes `<= 0` and `> 0` alike and lands in the arithmetic.
    bool is excluded because it subclasses int, so True would otherwise sail
    through as 1.0.
    """
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]()#+\-.!|<>~$])")


def _md_escape(value) -> str:
    """Neutralize markdown in a string this app did not author.

    st.warning/info/error/toast/spinner all render their text as markdown, and
    the messages below interpolate values from outside: TradingView
    descriptions and error strings, symbols that round-trip through
    positions.json, and raw text typed into a grid cell. Streamlit already
    strips HTML from these, so what is left is markdown itself -- links,
    images and formatting injected into the app's own UI.

    "$" is escaped for a reason that bites well-behaved input too: two dollar
    signs in one message form a LaTeX span that swallows everything between
    them (verified live), which is why several messages here avoid dollar
    amounts by hand. Newlines collapse to spaces so an interpolated value can
    never open a block construct -- heading, list, table or code fence -- when
    it lands at the start of a message.
    """
    return _MD_SPECIAL_RE.sub(r"\\\1", str(value)).replace("\n", " ")


# Excel and Sheets evaluate a text cell beginning with any of these as a
# formula the moment the file is opened.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Defuse a CSV cell a spreadsheet would execute instead of display.

    Only strings are touched, which is what keeps genuine negative numbers
    intact: -5.5 arrives here as a float, not "-5.5", so it exports as the
    number it is. The leading apostrophe is the spreadsheet convention for
    "this is text" and is stripped on display.
    """
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


TIMEFRAME_OPTIONS = {"Hourly": "1h", "Daily": "1D", "Weekly": "1W"}
DEFAULT_TIMEFRAME_LABEL = "Weekly"

DEFAULT_STATE = {
    # 0 means "never entered", not "a $0 portfolio". A fresh install must not
    # ship somebody else's number: this app is shared by copying the folder, and
    # a seeded figure would silently size a new user's trades against a
    # portfolio that isn't theirs. Everything downstream already handles it --
    # compute_outputs() returns None for portfolio_size <= 0, so every computed
    # column stays blank until a real value is typed.
    "portfolio_size": 0.0,
    # Left at 1%: unlike portfolio size this is a conventional starting point
    # rather than personal data, so a new install has a sane risk default.
    "risk_pct": 1.0,
    "timeframe_label": DEFAULT_TIMEFRAME_LABEL,
    "positions": [],
    "last_refreshed_at": None,
    # Bumped once per accepted write. A session may only overwrite the file it
    # loaded, so a second tab holding an older revision can't clobber a newer
    # one -- see save_state(). A file written before this existed has no
    # "revision" key and reads as 0, which is exactly the starting value, so
    # existing files migrate on their first save with no special casing.
    "revision": 0,
}


def default_state() -> dict:
    # {**DEFAULT_STATE} alone would alias the module-level "positions" list --
    # sessions mutate it in place, so each caller needs its own copy.
    return {**DEFAULT_STATE, "positions": []}


def _valid_position(pos) -> dict | None:
    """One position from positions.json, repaired -- or None if unusable.

    The table build hard-indexes pos["id"], ["symbol"], ["asset_class"],
    ["entry_price"], ["atr_multiple"] and ["tranches"], and formats several of
    them, so a row missing one raised KeyError and a row holding a string where
    a number belongs raised TypeError -- from the middle of the render, taking
    the entire page down on every load with no way back except editing the file
    by hand. json.loads only promises well-formed JSON, never this app's shape.
    """
    if not isinstance(pos, dict):
        return None
    text = {}
    for key in ("id", "symbol", "asset_class"):
        value = pos.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        text[key] = value.strip()
    atr_multiple = pos.get("atr_multiple")
    tranches = pos.get("tranches")
    entry_price = pos.get("entry_price")
    if not _finite_number(atr_multiple) or atr_multiple < ATR_MULTIPLE_MIN:
        return None
    if not _finite_number(tranches) or int(tranches) < 1:
        return None
    if not _finite_number(entry_price) or entry_price <= 0:
        return None
    atr_value = pos.get("atr_value")
    optional_text = ("exchange", "added_at", "display_name", "resolution_note",
                     "fetch_error")
    return {
        # Unrecognized keys ride along untouched: dropping them would silently
        # delete a field written by a newer version of this app.
        **pos,
        **text,
        "atr_multiple": float(atr_multiple),
        "tranches": int(tranches),
        "entry_price": float(entry_price),
        # Diagnostics only -- a wrong type here can't crash the table, but it
        # renders as a stray "None" or breaks a .upper(), so normalize instead
        # of rejecting an otherwise perfectly usable position. atr_value is
        # legitimately absent when a fetch has never succeeded for this row.
        "atr_value": float(atr_value) if _finite_number(atr_value) else None,
        **{k: (pos[k] if isinstance(pos.get(k), str) else None) for k in optional_text},
    }


def _validated_state(saved) -> tuple[dict, list[str]]:
    """positions.json coerced into this app's shape, plus what had to change.

    Returns ({state}, [notes]). An empty notes list means the file was already
    exactly as expected; anything in it means data was replaced or dropped and
    the caller must tell the user rather than quietly carry on.
    """
    if not isinstance(saved, dict):
        return default_state(), [
            f"the file contains a JSON {type(saved).__name__} where an object "
            "was expected, so nothing in it could be read."
        ]
    notes = []
    state = default_state()
    state.update({k: v for k, v in saved.items() if k != "positions"})

    size = state["portfolio_size"]
    # "< 0", not "<= 0": a plain 0 is the never-entered sentinel a fresh install
    # starts from, and it round-trips to disk on that install's first save.
    # Treating it as corruption made a brand-new install accuse its own file --
    # a "didn't match the expected format" banner plus a .invalid- backup on the
    # second launch. _finite_number already rejects bool/NaN/inf, so a real
    # negative is the only remaining way this can be wrong.
    if not _finite_number(size) or size < 0:
        notes.append(f"portfolio size ({size!r}) is not a positive number.")
        state["portfolio_size"] = DEFAULT_STATE["portfolio_size"]
    else:
        state["portfolio_size"] = float(size)

    risk = state["risk_pct"]
    if not _finite_number(risk) or not 0 < risk <= 100:
        notes.append(f"risk ({risk!r}) is not a percentage between 0 and 100.")
        state["risk_pct"] = DEFAULT_STATE["risk_pct"]
    else:
        state["risk_pct"] = float(risk)

    # Not merely "is a string": st.radio raises when its session-state value
    # isn't one of the options it was handed, so a bad label crashed the
    # sidebar before any of the page had rendered.
    if state["timeframe_label"] not in TIMEFRAME_OPTIONS:
        notes.append(
            f"ATR timeframe ({state['timeframe_label']!r}) is not one of "
            f"{', '.join(TIMEFRAME_OPTIONS)}."
        )
        state["timeframe_label"] = DEFAULT_TIMEFRAME_LABEL

    if not isinstance(state["last_refreshed_at"], (str, type(None))):
        state["last_refreshed_at"] = None

    revision = state["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        # A revision that isn't a whole number >= 0 can never equal what
        # _disk_revision() reads back, so every save would look like another
        # tab's newer file and this session could never write at all.
        notes.append(f"the save revision ({revision!r}) is not a whole number.")
        state["revision"] = 0

    raw_positions = saved.get("positions", [])
    if not isinstance(raw_positions, list):
        notes.append(
            f"the positions list is a JSON {type(raw_positions).__name__} "
            "rather than an array."
        )
        raw_positions = []
    positions = []
    seen_ids = set()
    dropped = 0
    for item in raw_positions:
        valid = _valid_position(item)
        # Ids are the grid's row identity: the inline-edit push-back matches
        # edited rows back to positions by id, so a duplicate would apply one
        # cell edit to two different positions at once.
        if valid is None or valid["id"] in seen_ids:
            dropped += 1
            continue
        seen_ids.add(valid["id"])
        positions.append(valid)
    if dropped:
        notes.append(
            f"{dropped} position(s) were unreadable (missing or non-numeric "
            "required fields, or a duplicate id) and were not loaded."
        )
    state["positions"] = positions
    return state, notes


def load_state() -> dict:
    if DATA_FILE.exists():
        try:
            saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            state, notes = _validated_state(saved)
            if notes:
                # Copy the file aside BEFORE returning: these notes mean values
                # were replaced or rows dropped, and the next accepted
                # save_state() writes that reduced state back over the
                # original. Copy rather than rename -- unlike the corrupt case
                # below, what's on disk is still partly usable, so the app
                # keeps using it.
                backup = DATA_FILE.with_name(
                    f"{DATA_FILE.name}.invalid-{datetime.now():%Y%m%d-%H%M%S}"
                )
                try:
                    backup.write_bytes(DATA_FILE.read_bytes())
                    os.chmod(backup, 0o600)
                    kept = f"The file as found was copied to {backup.name}."
                except OSError:
                    kept = "A backup copy could NOT be written."
                state["load_error"] = (
                    "positions.json didn't match the expected format: "
                    + " ".join(notes) + " " + kept
                )
            return state
        except json.JSONDecodeError as exc:
            # Never silently discard a corrupted-but-recoverable file: the next
            # save_state() would overwrite it for good. Move it aside first so
            # recovery stays possible, and tell the user what happened.
            backup = DATA_FILE.with_name(
                f"{DATA_FILE.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
            )
            try:
                DATA_FILE.rename(backup)
                note = f"the original was preserved as {backup.name}"
            except OSError:
                note = "the original could not be moved aside"
            state = default_state()
            state["load_error"] = (
                f"positions.json is corrupted ({exc}) — {note}. "
                "Starting with an empty portfolio."
            )
            return state
        except OSError as exc:
            # Unreadable (permissions/IO) is different from corrupted: the data
            # may be intact, so block saving entirely rather than risk
            # overwriting a good file we simply couldn't read.
            state = default_state()
            state["load_error"] = (
                f"positions.json could not be read ({exc}). Starting with an "
                "empty portfolio; saving is disabled to protect the file."
            )
            state["save_blocked"] = True
            return state
    return default_state()


def _state_payload() -> dict:
    return {
        "portfolio_size": st.session_state.portfolio_size,
        "risk_pct": st.session_state.risk_pct,
        "timeframe_label": st.session_state.timeframe_label,
        "positions": st.session_state.positions,
        "last_refreshed_at": st.session_state.get("last_refreshed_at"),
    }


def _snapshot(payload: dict) -> str:
    """Stable string form of the saved state, for "did anything change?"."""
    return json.dumps(payload, sort_keys=True, default=str)


def _disk_revision() -> int:
    """Revision currently in positions.json.

    0 for both "no file yet" and "a file written before revisions existed", so
    a fresh install and an upgrade both line up with a session's starting 0 and
    save normally. -1 for a file that exists but won't parse: that never equals
    a live session's revision, so the session treats itself as stale and
    re-reads (where load_state's corruption handling runs) instead of
    overwriting something it failed to understand.
    """
    if not DATA_FILE.exists():
        return 0
    try:
        return int(json.loads(DATA_FILE.read_text(encoding="utf-8")).get("revision", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return -1


def _atomic_write(state: dict) -> None:
    # mkstemp rather than a fixed "<name>.tmp": two sessions saving at once
    # shared that single path, and their interleaved writes could be renamed
    # into place as one blended file -- corruption, not just a stale write.
    fd, tmp_name = tempfile.mkstemp(dir=str(DATA_FILE.parent),
                                    prefix=DATA_FILE.name + ".", suffix=".tmp")
    try:
        # os.replace moves the temp file's own inode into place, so the
        # destination inherits *its* mode (0600 from mkstemp) -- without this
        # the file's permissions came from the umask instead, quietly widening
        # a deliberately chmod-ed positions.json back to world-readable.
        # fchmod is Unix-only (the os docs say "Availability: Unix") and this
        # app supports Python 3.10+, so on Windows the attribute is absent and
        # calling it raises AttributeError -- which save_state() does not catch,
        # crashing the page on a user's very first save. There is no POSIX mode
        # to widen there anyway: the file inherits its parent directory's ACL.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # allow_nan=False: Python emits bare NaN/Infinity, which no other
            # JSON reader accepts. Better to refuse the write (the good file
            # survives) than to persist something only Python can parse.
            json.dump(state, fh, indent=2, allow_nan=False)
            fh.flush()
            # Without fsync the rename can reach disk before the data does, so
            # a power loss leaves a valid-looking but empty file. The
            # write-then-rename alone only protects against a process crash.
            os.fsync(fh.fileno())
        os.replace(tmp_name, DATA_FILE)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def save_state() -> None:
    if st.session_state.get("save_blocked"):
        st.error(
            "Saving is disabled: positions.json could not be read at startup, "
            "and writing now could overwrite good data. Fix the file or its "
            "permissions and restart the app."
        )
        return
    payload = _state_payload()
    snapshot = _snapshot(payload)
    # Streamlit reruns for far more than edits -- refocusing the window, a
    # websocket reconnect, any widget interaction -- and this used to rewrite
    # the whole file on every one of them. That is what let a forgotten second
    # tab destroy a position without anyone touching it, so a write now
    # requires the state to have actually changed.
    if snapshot == st.session_state.get("_saved_snapshot"):
        return
    # Optimistic concurrency: we may only overwrite the exact revision we
    # loaded. If another tab has moved disk forward, this session's whole
    # snapshot is stale -- including the positions list, which is what made the
    # old blind write destructive -- so take the newer file instead.
    if _disk_revision() != st.session_state.get("revision", 0):
        st.session_state._reload_from_disk = True
        queue_toast(
            "Reloaded: this calculator was changed in another tab. "
            "Your last change was not saved — please redo it.", icon="🔄")
        st.rerun()
    revision = st.session_state.get("revision", 0) + 1
    try:
        _atomic_write({**payload, "revision": revision})
    except ValueError as exc:
        # Only allow_nan=False raises this. Surface it instead of letting it
        # reach the script body as a traceback; positions.json still holds the
        # last good state, which is the outcome we want.
        st.error(
            f"Could not save: the portfolio contains a value that is not a real "
            f"number ({exc}). positions.json was left unchanged."
        )
        return
    st.session_state.revision = revision
    st.session_state._saved_snapshot = snapshot


class RetryableUpstreamError(Exception):
    """Raised (not returned) for transient TradingView failures so st.cache_data
    never caches them -- a cached 429 would make the Retry button silently
    replay the failure for the full 60s TTL. Definitive results (success, or
    "not listed here") stay cached."""

    def __init__(self, error: dict):
        super().__init__(error.get("message", "upstream error"))
        self.error = error


@st.cache_data(ttl=60, show_spinner=False)
def fetch_atr(symbol: str, exchange: str, timeframe: str):
    """Returns (atr_info dict, error dict) — exactly one is None. Raises
    RetryableUpstreamError for transient upstream failures (never cached)."""
    result = analyze_coin(symbol.strip().upper(), exchange.strip().upper(), timeframe)
    if "error" in result:
        # analyze_coin still has paths that return a *string* here rather than
        # the usual dict (screener_service.py:680 and :719); :719 is a `return`
        # inside its own try block, so that function's `except` never converts
        # it. Calling .get() on the string raised AttributeError, which
        # _fetch_one doesn't catch (only RetryableUpstreamError), so it escaped
        # to the script body and rendered the whole page as a traceback --
        # losing the in-flight refresh, because save_state() runs after it.
        error = result["error"]
        if not isinstance(error, dict):
            error = {"message": str(error), "retryable": False}
        if error.get("retryable"):
            raise RetryableUpstreamError(error)
        return None, error
    atr = result.get("atr") or {}
    atr_value = atr.get("value")
    # Deliberately stricter than "is None". json.loads accepts bare NaN and
    # Infinity tokens, and upstream's _safe_round() passes NaN through
    # untouched, so a partial or non-numeric scanner row can hand us one. A NaN
    # here would be persisted by save_state(), pass every comparison guard, and
    # then crash max_safe_atr_multiple() on math.floor() -- on this and every
    # later page load, recoverable only by hand-editing positions.json.
    if not _finite_number(atr_value):
        return None, {"message": "TradingView returned no usable ATR value for this symbol/timeframe."}
    current_price = result.get("price_data", {}).get("current_price")
    return {
        "atr_value": atr_value,
        "resolved_exchange": result.get("resolved_exchange") or result.get("exchange"),
        # None is already the "no price came back" signal every caller handles,
        # so a non-finite price degrades into that path instead of a new one.
        "current_price": current_price if _finite_number(current_price) else None,
        "resolution_note": result.get("resolution_note"),
    }, None


# A single bounded pool for every TradingView HTTP call in the app, no matter how
# many layers of per-asset-class / per-exchange / per-position fanout submit to
# it, so checking one symbol (3 asset classes x 8 exchanges = up to 24 calls)
# doesn't spin up an uncapped number of simultaneous requests.
#
# The 429s that once made this necessary as a volume mitigation turned out to be
# caused by tradingview_ta's User-Agent header, not request volume (see the UA
# rewrite above) — 30 concurrent scanner requests were confirmed to succeed once
# that header is fixed. 8 (matching TRADINGVIEW_MCP_MAX_INFLIGHT above) is plenty
# of real headroom without going fully uncapped; callers must still expect and
# surface UPSTREAM_ERROR/retryable failures explicitly for genuine transient
# failures (see detect_asset_classes).
_HTTP_POOL = ThreadPoolExecutor(max_workers=8)


# Upstream marks retryable=True for exactly one thing: an exception during its
# single-shot fetch (screener_service.py:146 -- a 429, a reset, a truncated JSON
# body). Those pass in milliseconds, and until now app.py gave up on the first
# one, so a single unlucky request out of the ~8 a lookup fires was enough to
# show the user "TradingView is rate-limiting" and demand a manual Retry.
# Deliberately ONE retry, not a backoff ladder: if TradingView really is
# rate-limiting, more immediate requests add to the load that caused it, and an
# honest warning now beats the same warning three seconds later.
_RETRY_DELAY_S = 0.2
# Upstream sends retry_after_s=60 when a batched scan is wiped out
# (scanner_service.py:344). No retry inside a 2s lookup can satisfy that, so
# don't spend the user's time pretending otherwise.
_RETRY_AFTER_CEILING_S = 5.0


def _worth_retrying(error: dict, deadline: float | None) -> bool:
    retry_after = error.get("retry_after_s")
    if retry_after and retry_after > _RETRY_AFTER_CEILING_S:
        return False
    if deadline is None:
        return True
    # Only retry while the answer could still be USED. Past the wave's deadline
    # verify_search_candidates() has already stopped waiting for this candidate,
    # so the request would be pure extra load on an endpoint that just failed.
    return time.monotonic() + _RETRY_DELAY_S < deadline


def _fetch_one(symbol: str, exchange: str, timeframe: str, deadline: float | None = None):
    try:
        return fetch_atr(symbol, exchange, timeframe)
    except RetryableUpstreamError as exc:
        if not _worth_retrying(exc.error, deadline):
            return None, exc.error
    # Outside the except block: retrying inside it chains the original 429 onto
    # any traceback the retry raises, which is noise -- the first failure is
    # already known to be transient.
    time.sleep(_RETRY_DELAY_S)
    try:
        return fetch_atr(symbol, exchange, timeframe)
    except RetryableUpstreamError as exc:
        return None, exc.error


def _fetch_many(symbol: str, exchanges: list[str], timeframe: str) -> dict:
    """Fetch `exchanges` concurrently (each is an independent HTTP round-trip to
    TradingView, so this is purely I/O-bound and safe to parallelize) and return
    {exchange: (atr_info, error)}."""
    futures = {exch: _HTTP_POOL.submit(_fetch_one, symbol, exch, timeframe) for exch in exchanges}
    return {exch: fut.result() for exch, fut in futures.items()}


def resolve_exchange_and_fetch(symbol: str, asset_class: str, timeframe: str):
    """Try the top 3 exchanges for asset_class live, then up to 5 more, until one
    actually lists the symbol. Returns (exchange, atr_info, error) — atr_info is
    None only if every candidate exchange failed (error holds the last failure).

    All 8 candidates are fetched concurrently — these are independent network
    calls, and waiting for wave 1 to fully finish before starting wave 2 was the
    main source of latency when a symbol only lists on a lower-priority exchange.
    Once every result is in, the highest-priority success wins even though a
    lower-priority one may have answered first."""
    priority = EXCHANGE_PRIORITY.get(asset_class, EXCHANGE_PRIORITY["Crypto"])
    candidates = exchange_candidates(asset_class)  # top 3 first, then up to 5 more as fallback
    results = _fetch_many(symbol, candidates, timeframe)
    last_error = None
    for exch in candidates:
        atr_info, error = results[exch]
        if error is not None:
            # A retryable upstream failure must win the aggregation: if the one
            # exchange that lists this symbol returned a 429, a later
            # candidate's definitive "not listed" must not downgrade the
            # summary to "symbol doesn't exist".
            if last_error is None or (error.get("retryable") and not last_error.get("retryable")):
                last_error = error
            continue
        resolved = atr_info.get("resolved_exchange") or exch
        if resolved not in priority:
            # analyze_coin() silently falls back to a global symbol search when
            # `symbol` isn't listed on the requested exchange, and returns SUCCESS
            # on whatever exchange actually has it -- e.g. asking for HYPEUSDT on
            # NASDAQ (Stock class) succeeds with resolved_exchange="KUCOIN". That's
            # a real symbol, but not a match for THIS asset class, so treating
            # "atr_info is not None" as a match made every asset class appear to
            # match every symbol that exists anywhere on TradingView. Only accept
            # a result whose resolved exchange is actually one of asset_class's own.
            if last_error is None or not last_error.get("retryable"):
                last_error = {"message": f"{symbol} is not listed on any {asset_class} exchange checked."}
            continue
        return resolved, atr_info, None
    # No exchange on total failure: returning a real venue here (the old
    # candidates[-1]) let callers persist an exchange the symbol was never
    # actually verified on.
    return None, None, last_error or {"message": f"{symbol} was not found on any {asset_class} exchange."}


def _refresh_one(pos: dict, timeframe: str):
    """Re-fetch ONE saved position, in resolve_exchange_and_fetch()'s own
    (exchange, atr_info, error) shape.

    Asks the exchange already stored on the position before falling back to the
    8-candidate fan-out. That venue was VERIFIED when the position was added, so
    re-running exchange DISCOVERY on every refresh re-asked 8 exchanges a
    question already answered -- and since it all funnels through the 8-worker
    _HTTP_POOL, 4 positions x 8 candidates = 32 requests = 4 SEQUENTIAL waves,
    making the wall clock 4x one call's latency instead of 1x. Measured at
    2.5s/call: 10.0s before, 2.5s here. That 4x is why switching timeframe could
    hang for ~10s on a slow moment while any single lookup still felt instant.
    It also stops the fan-out asking NASDAQ first for an NYSE-listed position,
    which is what printed "not listed on NASDAQ; analysis was run on NYSE" on
    every refresh.

    The fan-out stays the fallback, so a symbol that moved venue, was renamed or
    was delisted still resolves exactly as it did before -- only the common path
    is new.
    """
    stored = pos.get("exchange")
    if stored:
        # Submitted to _HTTP_POOL rather than called inline: this already runs on
        # refresh_all_positions' per-position pool, so fetching directly here
        # would put len(positions) requests on the wire at once and escape the
        # single global cap every other fetch path in this app respects.
        atr_info, _stored_error = _HTTP_POOL.submit(
            _fetch_one, pos["symbol"], stored, timeframe).result()
        if atr_info is not None:
            resolved = (atr_info.get("resolved_exchange") or stored).upper()
            # Two conditions, and BOTH are load-bearing:
            #
            # resolved == stored -- analyze_coin() silently falls back to a
            # global symbol search when the requested venue doesn't list the
            # symbol, so a "success" here can actually be some OTHER exchange
            # answering. Accepting that would quietly downgrade the position's
            # venue: the fan-out picks the highest-PRIORITY success (Binance
            # before Kucoin), while a self-corrected single call just reports
            # wherever the symbol happened to be found. Measured: a position
            # stored on BITFINEX came back resolved to KUCOIN and was accepted,
            # where the fan-out would have restored BINANCE. So an inequality
            # means the stored venue is stale -- fall through and re-discover
            # properly by priority.
            #
            # The class-membership check is still needed on top: a hand-edited
            # or legacy positions.json can name a venue that genuinely lists the
            # symbol but belongs to another asset class (NASDAQ under Crypto).
            if resolved == stored.upper() and resolved in EXCHANGE_PRIORITY.get(
                    pos["asset_class"], EXCHANGE_PRIORITY["Crypto"]):
                return resolved, atr_info, None
        # _stored_error is deliberately dropped rather than returned: the
        # fan-out below re-attempts this same venue and its aggregation decides
        # the final error, which is what keeps a retryable failure from being
        # reported as a definitive "not found".
    return resolve_exchange_and_fetch(pos["symbol"], pos["asset_class"], timeframe)


def refresh_all_positions() -> None:
    """Live-refresh price + ATR for every saved position (concurrently) and store
    the results directly on each position dict, which save_state() persists to
    positions.json. This is the ONLY place (besides adding a new position) that
    calls TradingView for already-saved positions — the Positions table below
    just reads these stored fields, so reloading the page or any other rerun
    never triggers a network call."""
    positions = st.session_state.positions
    if not positions:
        st.session_state.last_refresh_changes = []
        return
    timeframe = TIMEFRAME_OPTIONS[st.session_state.timeframe_label]
    for pos in positions:
        # A refresh must bypass the 60s TTL, and the entry serving this symbol
        # can be keyed by ANY candidate exchange (analyze_coin falls back
        # internally, so e.g. the BINANCE-keyed entry can hold the OKX result).
        # Clear every candidate key for this symbol -- but never the whole
        # cache, which would also discard an unrelated, still-fresh
        # "Add position" symbol check in progress.
        venues = set(exchange_candidates(pos["asset_class"]))
        # The stored exchange is cleared too, and not just because it is usually
        # already in the candidate list: it can sit OUTSIDE the top-8 window
        # (Stock priority lists 13 venues), and _refresh_one now reads exactly
        # that key first -- leaving it cached would make a refresh hand back the
        # very value it was asked to replace.
        if pos.get("exchange"):
            venues.add(pos["exchange"])
        for exch in venues:
            fetch_atr.clear(pos["symbol"], exch, timeframe)
    with ThreadPoolExecutor(max_workers=len(positions)) as pool:
        futures = {
            pos["id"]: pool.submit(_refresh_one, pos, timeframe)
            for pos in positions
        }
        results = {pid: fut.result() for pid, fut in futures.items()}
    changes = []
    for pos in positions:
        old_entry_price = pos.get("entry_price")
        old_atr_value = pos.get("atr_value")
        exchange, atr_info, error = results[pos["id"]]
        if atr_info is not None:
            # Only overwrite the stored exchange on a verified success -- on
            # total failure the resolver has no exchange to report, and the
            # stale-but-usable row below must keep its last known venue.
            pos["exchange"] = exchange
            pos["atr_value"] = atr_info["atr_value"]
            if atr_info["current_price"] is not None:
                pos["entry_price"] = atr_info["current_price"]
            pos["resolution_note"] = atr_info["resolution_note"]
            pos["fetch_error"] = None
            pos["added_at"] = datetime.now().strftime("%Y-%m-%d, %I:%M:%S %p")
        else:
            # Keep the last known good atr_value/entry_price (stale but usable)
            # instead of blanking the row on a transient TradingView failure.
            pos["fetch_error"] = error.get("message", str(error)) if error else "Unknown error"
        if pos.get("entry_price") != old_entry_price or pos.get("atr_value") != old_atr_value:
            changes.append({
                "id": pos["id"], "symbol": pos["symbol"],
                "old_entry_price": old_entry_price, "new_entry_price": pos.get("entry_price"),
                "old_atr_value": old_atr_value, "new_atr_value": pos.get("atr_value"),
            })
    st.session_state.last_refresh_changes = changes
    st.session_state.last_refreshed_at = datetime.now().strftime("%Y-%m-%d, %I:%M:%S %p")
    save_state()


def detect_asset_classes(
    symbol: str, timeframe: str, classes: list[str] | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Try resolving `symbol` against every asset class's exchange list (or just
    `classes`, when the user pre-picked one). Returns (matches, errors):
    matches has one entry per asset class that actually has live data for this
    symbol — so the caller can auto-pick when there's exactly one, or ask when
    there's more than one. errors carries the failure for every class that
    didn't match, keyed by asset class, so a caller can tell a genuine "not
    listed here" result apart from an upstream failure (error["retryable"] is
    True for the latter) instead of both looking identical as an empty
    `matches` list. Checking a single pre-picked class instead of all 3 cuts
    the TradingView request count for this lookup by 3x and structurally rules
    out cross-class collisions (e.g. a crypto ticker that happens to also be an
    unrelated stock symbol). Independent classes are resolved concurrently
    rather than one after another."""
    classes = classes or ASSET_CLASSES
    symbols = {ac: (with_default_quote(symbol) if ac == "Crypto" else symbol) for ac in classes}
    with ThreadPoolExecutor(max_workers=len(classes)) as pool:
        futures = {ac: pool.submit(resolve_exchange_and_fetch, symbols[ac], ac, timeframe) for ac in classes}
        results = {ac: fut.result() for ac, fut in futures.items()}
    matches = []
    errors = {}
    for ac in classes:
        exchange, atr_info, error = results[ac]
        if atr_info is not None:
            matches.append({"asset_class": ac, "exchange": exchange, "atr_info": atr_info, "symbol": symbols[ac]})
        elif error is not None:
            errors[ac] = error
    return matches, errors


_EM_TAG_RE = re.compile(r"</?em>")


@st.cache_data(ttl=60, show_spinner=False)
def _tradingview_symbol_search(query: str) -> list[dict]:
    """Raw hits from TradingView's public symbol search, used both to resolve
    a typed name to a ticker and to look up a ticker's human-readable name.
    Cached like fetch_atr() -- re-checking the same symbol (e.g. after ATR
    multiple/tranches resets the check) shouldn't re-hit this endpoint for a
    name/ticker mapping that hasn't changed. Raises RequestException/ValueError
    on failure -- st.cache_data does not cache exceptions, so a transient
    outage is never replayed from cache for the 60s TTL, and each caller
    decides whether a failure is cosmetic (display name) or must be surfaced
    (name-based search).
    Every text field TradingView returns wraps the matched substring in
    <em></em> (highlighting for their own UI) -- always strip via
    _EM_TAG_RE before using a field, or a query like "BTC" comes back as the
    literal string "<em>BTCUSD</em>" instead of "BTCUSD"."""
    resp = requests.get(
        "https://symbol-search.tradingview.com/symbol_search/",
        params={"text": query, "hl": 1, "lang": "en", "domain": "production"},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tradingview.com/",
            "Origin": "https://www.tradingview.com",
        },
        timeout=5,
    )
    resp.raise_for_status()
    payload = resp.json()
    # This endpoint is undocumented and has shipped more than one response
    # shape (a bare array, and an object wrapping "symbols"), while both
    # callers index straight into it: results[0]["symbol"] and r.get(...).
    # A shape change therefore raises KeyError/TypeError/AttributeError --
    # none of which either caller's except clause catches, since both expect
    # RequestException/ValueError -- so it escaped as a full-page traceback.
    # Normalize here, at the trust boundary, and let a genuinely
    # unrecognizable payload raise ValueError so the callers' existing
    # handling (retryable warning, or "no display name") applies unchanged.
    if isinstance(payload, dict):
        payload = payload.get("symbols", payload.get("data"))
    if not isinstance(payload, list):
        raise ValueError(
            f"symbol search returned {type(payload).__name__}, expected a list"
        )
    # Keep string fields and string-lists, drop everything else: that lets
    # callers treat .get("description", "") and ["symbol"] as strings without
    # re-checking (_EM_TAG_RE.sub() raises TypeError on a non-string), while
    # preserving "typespecs" -- a LIST that _candidate_asset_class() needs to
    # tell a real spot pair from a CFD, an index or a Glassnode metric. An entry
    # with no usable symbol is not a search hit at all.
    return [
        {k: v for k, v in _string_fields(item).items()}
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("symbol"), str)
    ]


def _string_fields(item: dict) -> dict:
    out = {}
    for key, value in item.items():
        if isinstance(value, str):
            out[key] = value
        elif isinstance(value, list):
            out[key] = [v for v in value if isinstance(v, str)]
    return out


# TradingView's own instrument taxonomy, which classifies a search hit far more
# reliably than any exchange allowlist can -- and that is what makes "find it on
# ANY exchange" safe to do. A live search for "SOLANA" returns 50 hits of which
# only 20 are the coin: 13 are 'fundamental' Glassnode metrics, one is an
# unrelated 'stock' (Solana Biofuels Ltd), one a synthetic 'index', two are
# Trade Nation CFD wrappers -- and the CFD is what results[0] used to pick.
_CANDIDATE_TYPE_RANK = {"spot": 0, "stock": 1, "fund": 1, "dr": 1, "futures": 2, "swap": 3}
_DERIVATIVE_TYPESPECS = {"cfd", "synthetic"}
# One fetch per candidate, and _HTTP_POOL has 8 workers -- so the whole
# verification is a single concurrent wave, never a second round-trip deep.
_MAX_VERIFIED_CANDIDATES = 8
_MAX_CANDIDATES_PER_ASSET = 4
_MAX_MATCH_CHOICES = 6
# Wall-clock ceiling for the verification wave. The whole lookup budget is 2s
# and the search leg ahead of it costs ~0.2-0.5s (twice that when it refines by
# base ticker), so this leaves headroom rather than spending it.
_VERIFY_BUDGET_S = 1.2


def _candidate_asset_class(hit: dict) -> str | None:
    """Which of this app's asset classes a raw search hit is tradeable in, or
    None when it isn't a position-sizeable instrument at all."""
    hit_type = hit.get("type", "")
    typespecs = {spec.lower() for spec in hit.get("typespecs") or []}
    if "crypto" in typespecs:
        # 'fundamental' metrics and 'index' aggregates carry the crypto typespec
        # too, and cfd/synthetic mark a wrapper rather than the coin itself.
        if hit_type not in ("spot", "swap") or typespecs & _DERIVATIVE_TYPESPECS:
            return None
        return "Crypto"
    if "commodity" in typespecs or hit_type == "futures":
        return "Commodity"
    if hit_type in ("stock", "fund", "dr") and not typespecs & _DERIVATIVE_TYPESPECS:
        return "Stock"
    return None


def _query_names(query: str, description: str) -> bool:
    """Does `query` actually NAME this listing, or did TradingView merely
    fuzzy-match it? Substring is far too loose: "SOL" sits inside "Solstice
    Advanced Materials", "Solventum" and "2x Solana ETF", so a substring test
    opened a six-way picker of unrelated tickers on an ordinary coin lookup.
    Whole-word is what separates "blackrock" genuinely naming BlackRock TCP
    Capital Corp. from "sol" merely prefixing Solstice."""
    if not query or not description:
        return False
    return re.search(rf"\b{re.escape(query)}\b", description, re.IGNORECASE) is not None


def rank_search_candidates(hit_lists: list[list[dict]], picked_class: str, query: str = "") -> list[dict]:
    """Turn raw search hits into verifiable (symbol, exchange, asset_class)
    candidates, best first.

    Ranking is this app's own preference order, never TradingView's relevance
    order -- a relevance ranking says what best matches the text typed, not what
    this app can actually price, which is why hit #1 for "SOLANA" was a CFD
    while the usable SOLUSD sat at #6. Exchanges outside EXCHANGE_PRIORITY sort
    last but are still kept: a legitimate coin must stay reachable even when the
    only venue listing it isn't one this app ranks."""
    ranked, seen = [], set()
    # Relevance is per-search, so the counter restarts for each list: the second
    # pass searches the REFINED ticker deliberately, and its hits are better
    # answers than the first pass's, not worse ones. Scoring them by position in
    # a concatenated list would rank every refined hit below every original hit
    # -- which left "jupiter" on Coinbase's JUPITERUSD while "JUP" reached
    # Binance, the exact name-vs-ticker split the second pass exists to close.
    for hits in hit_lists:
        for order, hit in enumerate(hits):
            asset_class = _candidate_asset_class(hit)
            if asset_class is None:
                continue
            symbol = _EM_TAG_RE.sub("", hit["symbol"]).upper()
            exchange = _EM_TAG_RE.sub("", hit.get("exchange", "")).upper()
            if not exchange or (symbol, exchange) in seen:
                continue
            seen.add((symbol, exchange))
            priority = EXCHANGE_PRIORITY.get(asset_class, [])
            # Take the quote currency from TradingView rather than matching the
            # symbol's tail against CRYPTO_QUOTE_CURRENCIES: that split is
            # genuinely ambiguous and gets it wrong. "ARBUSD" ends with both
            # "USD" (Arbitrum, correct) and "BUSD" (leaving "AR" -- Arweave, a
            # different coin), and BUSD sorts first, so a search for "arbitrum"
            # refined itself into ARUSDT. currency_code says "USD" outright.
            quote = _EM_TAG_RE.sub("", hit.get("currency_code", "")).upper()
            base = symbol[: -len(quote)] if quote and symbol.endswith(quote) and len(symbol) > len(quote) else symbol
            description = _EM_TAG_RE.sub("", hit.get("description", "")).strip()
            ranked.append({
                "symbol": symbol,
                "exchange": exchange,
                "asset_class": asset_class,
                "base": base,
                "description": description,
                "_named": _query_names(query, description),
                "_class_rank": 0 if picked_class in ("All", asset_class) else 1,
                "_type_rank": _CANDIDATE_TYPE_RANK.get(hit.get("type", ""), 9),
                "_exchange_rank": priority.index(exchange) if exchange in priority else len(priority),
                "_order": order,
            })
    # EXCHANGE_PRIORITY is a VENUE preference, so it may only order listings OF
    # THE SAME ASSET. Between two different companies it means nothing, and
    # letting it decide is exactly what silently resolved "blackrock" to TCPC
    # (NASDAQ, rank 0) instead of BLK (NYSE, rank 1) -- both plain stocks, so
    # the sort fell straight through to the venue. Distinct assets are ordered
    # by TradingView's own relevance instead (the earliest hit mentioning that
    # asset); venue priority then picks the listing within each asset, which is
    # what keeps SOLUSDT@BINANCE beating SOLUSD@COINBASE for the same coin.
    groups: dict[tuple[str, str], dict] = {}
    for candidate in ranked:
        key = (candidate["asset_class"], candidate["base"])
        group = groups.setdefault(key, {"order": candidate["_order"], "named": False})
        group["order"] = min(group["order"], candidate["_order"])
        # Only a real spot/stock listing can make an asset worth offering as a
        # separate choice. A perpetual is not a different company: "SOLUSDT.P"
        # keeps its ".P" through the quote-currency strip, so it lands in its own
        # base group, and its description ("Cardano / TetherUS PERPETUAL
        # CONTRACT") name-matches just as well as the spot pair's -- offering it
        # would ask the user to choose between a coin and its own future.
        group["named"] = group["named"] or (candidate["_named"] and candidate["_type_rank"] <= 1)
    # A query that IS a ticker is unambiguous by construction: "AAPL" means AAPL,
    # not Direxion's 2x AAPL ETF whose description names it too. The
    # several-companies prompt is a NAME feature, so an exact ticker hit turns it
    # off and restores the plain one-best-per-class behaviour.
    exact_ticker = any(base == query.upper() for _, base in groups) if query else False
    for candidate in ranked:
        group = groups[(candidate["asset_class"], candidate["base"])]
        # Name-match is a property of the ASSET, not of one listing: Coinbase
        # describes the pair "Solana / US Dollar" while Binance calls the very
        # same coin "SOL / TetherUS", so judging per-listing would mark the
        # winning Binance listing unnamed and drop the coin from the prompt.
        candidate["_named"] = group["named"] and not exact_ticker
        candidate["_rank"] = (
            candidate["_class_rank"],
            candidate["_type_rank"],
            group["order"],
            candidate["_exchange_rank"],
            candidate["_order"],
        )
    ranked.sort(key=lambda candidate: candidate["_rank"])
    return ranked


def search_candidates(query: str, picked_class: str) -> list[dict]:
    """Ranked candidates for `query`, refined by one extra search on the
    resolved base ticker when the best crypto hit isn't already on the
    top-priority exchange.

    TradingView's search matches typed text against each listing's description,
    and Binance describes its pair as "SOL / TetherUS" while Coinbase describes
    its own as "Solana / US Dollar" -- so a search for "solana" returns zero
    Binance pairs while a search for "SOL" returns them first (verified live).
    Without this second pass the exchange preference below would apply only to
    ticker queries, and typing a coin's name would silently resolve it to a
    different venue than typing its ticker. Gated on the best hit not already
    being top-priority, so ticker queries and names that already resolve to
    Binance pay nothing for it."""
    hits = _tradingview_symbol_search(query)
    candidates = rank_search_candidates([hits], picked_class, query)
    if not candidates or candidates[0]["asset_class"] != "Crypto":
        return candidates
    if candidates[0]["_exchange_rank"] == 0:
        return candidates
    # The most common base across the crypto candidates, not just the top hit's:
    # Coinbase names Jupiter's pair JUPITERUSD (base "JUPITER", identical to the
    # query, so refining on it would just repeat the same search) while every
    # other venue calls it JUP. Taking the consensus finds the real ticker and
    # reaches Binance; Counter ties break by insertion order, which is rank
    # order, so the best-ranked base still wins a tie.
    bases = Counter(c["base"] for c in candidates if c["asset_class"] == "Crypto")
    # Skip PAST a base identical to the query rather than giving up on it: that
    # base is the unrefined query itself, so re-searching it would just repeat
    # the search we already ran. Coinbase is the only venue naming Jupiter's
    # pair JUPITERUSD (base "JUPITER" -- the query), so once it wins the tie the
    # old "give up" reading stopped the refinement dead and left "jupiter" on
    # Coinbase while "JUP" reached Binance.
    base = next((b for b, _ in bases.most_common() if b != query.upper()), None)
    if base is None:
        return candidates
    try:
        # Kept as a SEPARATE list, never concatenated: relevance restarts per
        # search (see rank_search_candidates), and _tradingview_symbol_search is
        # @st.cache_data-backed, so building a new list is also what keeps the
        # cached one unmutated.
        refined = _tradingview_symbol_search(base)
    except (requests.RequestException, ValueError):
        # Refinement only. The first search already produced usable candidates,
        # so a failure here can change WHICH venue wins but never WHETHER the
        # coin is found -- surfacing it would turn a working lookup into a
        # user-visible error for no gain.
        return candidates
    # Still ranked against the ORIGINAL query, not the refined base: name-match
    # asks what the user typed, and refining "solana" to "SOL" would otherwise
    # rewrite the question being answered.
    return rank_search_candidates([hits, refined], picked_class, query)


def shortlist_candidates(candidates: list[dict]) -> list[dict]:
    """The candidates actually worth a round-trip, taken round-robin across
    distinct ASSETS: crypto lists a popular coin on a dozen exchanges, and a
    straight top-8 slice would let it crowd out every other match -- both the
    cross-class ambiguity prompt and the several-companies prompt the UI
    depends on. Round-robin rather than one-per-asset because tier 1+ keeps
    each asset's runner-up venues in the wave, so an asset whose best venue
    fails to answer is still found on its second -- the property the 100%
    find rate rests on."""
    by_asset: dict[tuple[str, str], list[dict]] = {}
    for candidate in candidates:
        by_asset.setdefault((candidate["asset_class"], candidate["base"]), []).append(candidate)
    # Only assets that could actually be OFFERED are worth a round-trip: the best
    # asset of each class (what the cross-class prompt needs) plus every asset the
    # query names (what the several-companies prompt needs). candidates arrive
    # ranked and dicts keep insertion order, so the first group of a class is that
    # class's best. Verifying the rest would spend the user's 2s budget fetching
    # fuzzy matches that nothing will ever display.
    chosen, seen_classes = [], set()
    for (asset_class, _), group in by_asset.items():
        if asset_class not in seen_classes or group[0]["_named"]:
            chosen.append(group)
            seen_classes.add(asset_class)
    shortlist = []
    for tier in range(_MAX_CANDIDATES_PER_ASSET):
        for group in chosen:
            if tier < len(group):
                shortlist.append(group[tier])
                if len(shortlist) >= _MAX_VERIFIED_CANDIDATES:
                    return shortlist
    return shortlist


def verify_search_candidates(shortlist: list[dict], timeframe: str) -> tuple[list[dict], dict]:
    """Fetch every shortlisted candidate on its OWN exchange, concurrently, and
    return (matches, errors) in the shape detect_asset_classes() produces.

    Going straight to the exchange the search named is what decouples find rate
    from EXCHANGE_PRIORITY: resolve_exchange_and_fetch() has to guess, so it
    tries 8 exchanges per class and rejects anything resolving outside that
    list, while this knows the venue up front and needs one round-trip. The
    cross-class false matches that guard existed to stop are ruled out earlier
    and more precisely here, by _candidate_asset_class()."""
    # These run concurrently, so the wave costs the SLOWEST candidate, not the
    # sum -- and a single sluggish venue was enough to push an otherwise ~1.1s
    # lookup to 2.75s while the answer was already in hand. Cap the wait: a
    # candidate that hasn't answered by the deadline is treated as a retryable
    # miss (it may well be fine, we just aren't spending the user's time on it),
    # and the better-ranked results already collected still win.
    #
    # Computed BEFORE submitting, not after: the workers start on submit, so a
    # deadline taken afterwards would hand each retry a budget it has already
    # partly spent.
    deadline = time.monotonic() + _VERIFY_BUDGET_S
    futures = [
        _HTTP_POOL.submit(_fetch_one, candidate["symbol"], candidate["exchange"], timeframe, deadline)
        for candidate in shortlist
    ]
    matches, errors = {}, {}
    for candidate, future in zip(shortlist, futures):
        try:
            atr_info, error = future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            errors.setdefault(candidate["asset_class"], {
                "message": f"{candidate['symbol']} on {candidate['exchange']} did not respond in time.",
                "retryable": True,
                # Distinguishes OUR budget expiring from an upstream refusal.
                # Both are retryable, but only one of them is TradingView's
                # fault, and the warning used to blame rate-limiting for both.
                "timeout": True,
            })
            continue
        if atr_info is None:
            if error is not None:
                errors.setdefault(candidate["asset_class"], error)
            continue
        # One match per distinct ASSET, not per asset class. Keying on the class
        # collapsed BLK, TCPC and BRC into a single "Stock" match, so the UI's
        # "matches more than one" prompt could never fire for several companies
        # and the pipeline just picked whichever the sort happened to favour.
        # Keying on (class, base) still collapses one coin's many venues --
        # SOLUSDT@BINANCE and SOLUSD@COINBASE are the same asset, and the user
        # must not be asked to choose a venue -- while keeping genuinely
        # different companies apart. shortlist is ranked, so the first hit for
        # an asset is that asset's best venue.
        matches.setdefault((candidate["asset_class"], candidate["base"]), {
            "asset_class": candidate["asset_class"],
            "exchange": atr_info.get("resolved_exchange") or candidate["exchange"],
            "atr_info": atr_info,
            "symbol": candidate["symbol"],
            "base": candidate["base"],
            "description": candidate.get("description", ""),
            "named_match": candidate.get("_named", False),
        })
    # Errors stay keyed by asset class (the UI reports failures per class), so
    # the "already matched" filter has to compare against the classes that
    # matched, not against the now-two-part match keys.
    matched_classes = {asset_class for asset_class, _ in matches}
    return list(matches.values()), {ac: err for ac, err in errors.items() if ac not in matched_classes}


_TICKER_LIKE_RE = re.compile(r"^[A-Z0-9.]{2,10}\s*/\s*[A-Z0-9.]{2,10}$")


def lookup_display_name(symbol: str, exchange: str) -> str | None:
    """Best-effort human-readable name for `symbol` (e.g. "Apple Inc." for
    AAPL, "Solana / US Dollar" for SOLUSD), shown alongside the raw ticker in
    the Add-position confirmation. Purely cosmetic: None on any lookup miss,
    never blocks adding the position."""
    try:
        results = _tradingview_symbol_search(symbol)
    except (requests.RequestException, ValueError):
        # Cosmetic-only consumer: a failed lookup means "no display name",
        # never an existence claim, so swallowing here is the documented
        # intent -- the position still adds with the raw ticker.
        return None
    descriptions = [_EM_TAG_RE.sub("", r.get("description", "")) for r in results]
    descriptions = [d for d in descriptions if d]
    if not descriptions:
        return None
    match = next((r for r in results if r.get("exchange", "").upper() == exchange.upper()), None)
    # .get, not [""]: a hit can carry a symbol and exchange but no usable
    # description, and this line sits outside the try above -- so indexing it
    # took the whole page down for a purely cosmetic lookup.
    exchange_desc = _EM_TAG_RE.sub("", match.get("description", "")) if match else None
    # Some exchanges' own description is just the ticker split apart (e.g.
    # Binance's "SOL / USD" for SOLUSD) rather than an actual name -- fall back
    # to the best name from any exchange (e.g. Coinbase's "Solana / US Dollar")
    # instead of showing the user their own symbol back at them.
    if exchange_desc and not _TICKER_LIKE_RE.match(exchange_desc):
        return exchange_desc
    return next((d for d in descriptions if not _TICKER_LIKE_RE.match(d)), exchange_desc or descriptions[0])


def _symbol_key() -> str:
    """Session-state key for the Symbol input. The epoch suffix exists so
    add_position() can clear the field by ROTATING the key: assigning "" to a
    live text_input's key is accepted server-side, but the browser keeps
    showing its committed text (verified live), so the field never visibly
    cleared. A new key mounts a brand-new, genuinely empty widget instead."""
    return f"add_symbol_{st.session_state.get('add_form_epoch', 0)}"


def _portfolio_size_set() -> bool:
    """Whether a real portfolio size has been entered yet.

    0 is the never-entered sentinel (see DEFAULT_STATE), which is also exactly
    what compute_outputs() rejects -- so this is the same invariant that decides
    whether a row can show numbers, reused to decide whether the lookup that
    creates that row is worth doing at all.
    """
    return st.session_state.get("portfolio_size", 0.0) > 0


_NO_PORTFOLIO_HINT = "Enter your Portfolio Size in the sidebar first"


def check_symbol() -> None:
    """on_change callback: just stages the request. Streamlit runs on_change
    callbacks before the page re-renders, so doing the actual TradingView
    round-trips here would block with no spinner/progress shown at all — the
    page would look frozen for as long as the lookup takes. Staging it and
    doing the real work in run_pending_symbol_check() (called from the main
    script body, under st.spinner) is what makes the "please wait" state
    visible."""
    symbol = st.session_state.get(_symbol_key(), "").strip().upper()
    if not symbol:
        st.session_state.symbol_check = None
        st.session_state.pending_symbol_check = None
        return
    # Gated HERE rather than only on the Search button, because this callback has
    # three entry points -- the button, Enter/blur in the Symbol field, and the
    # Asset Class selectbox -- and disabled= reaches only the first. Pressing
    # Enter with no portfolio size ran a full TradingView lookup and added a
    # position whose every computed column was blank ("can't compute position
    # size"), which is a dead end the user cannot act on from that screen.
    # queue_toast, not st.toast: the button path reruns immediately after this
    # call and would throw a same-run toast away (see queue_toast).
    if not _portfolio_size_set():
        st.session_state.symbol_check = None
        st.session_state.pending_symbol_check = None
        queue_toast(f"{_NO_PORTFOLIO_HINT} — position sizes are a % of it.", "⚠️")
        return
    st.session_state.pending_symbol_check = {
        "symbol": symbol,
        "picked_class": st.session_state.get("add_asset_class_pick", "All"),
    }


def reset_symbol_check() -> None:
    """on_change callback for ATR multiple / # Tranches: they don't change which
    symbol/exchange matched, so they don't need a live TradingView re-check --
    but they DO change the duplicate-detection settings, so clear the stale
    verification and require pressing Enter (or Search) again rather than
    silently carrying it forward under new criteria."""
    st.session_state.symbol_check = None
    st.session_state.pending_symbol_check = None


def _clear_grid_selection() -> None:
    """Drop every checked row in the positions table.

    Remounting the grid under a new key is the ONLY thing that clears
    ag-grid's client-side selection -- see the note on the AgGrid key for why
    a custom getRowId cannot, and why the remove path has always done this.

    Called by the actions that change what the table CONTAINS or what its
    numbers MEAN, so a selection the user has forgotten about cannot outlive
    the rows it was pointing at: add, remove, refresh, and a timeframe switch.
    Deliberately NOT called for a search, an inline cell edit, or a
    portfolio/risk keystroke -- a remount also resets scroll, sort and column
    widths, which is a poor trade for an action that leaves the rows alone (and
    for the debounced settings fields would fire mid-typing)."""
    st.session_state.grid_mount = st.session_state.get("grid_mount", 0) + 1


def add_position(match: dict) -> None:
    """Add-position on_click callback. Runs before the next rerun renders any
    widget -- the only point Streamlit allows assigning a live widget's key.
    The old in-body `st.session_state.pop("add_symbol")` never cleared the
    field: popping only deletes the server-side copy, and the browser
    re-reports a still-rendered widget's value on the next run."""
    atr_info = match["atr_info"]
    st.session_state.positions.append({
        "id": uuid.uuid4().hex,
        "symbol": match["symbol"],
        "asset_class": match["asset_class"],
        "exchange": match["exchange"],
        "entry_price": atr_info["current_price"],
        "atr_value": atr_info["atr_value"],
        "display_name": match.get("display_name"),
        "resolution_note": atr_info["resolution_note"],
        "fetch_error": None,
        "atr_multiple": st.session_state.add_atr_multiple,
        "tranches": int(st.session_state.add_tranches),
        "added_at": datetime.now().strftime("%Y-%m-%d, %I:%M:%S %p"),
    })
    st.session_state.last_refreshed_at = datetime.now().strftime("%Y-%m-%d, %I:%M:%S %p")
    # Adding clears the symbol field and the whole check block with it, so
    # without this the only feedback is a row quietly appearing in a table the
    # user may have scrolled past. Rendered where the "found" message was, so
    # the confirmation replaces it in place instead of moving the page again.
    name_suffix = f" ({_md_escape(match['display_name'])})" if match.get("display_name") else ""
    st.session_state.add_success = (
        f"✅ {_md_escape(match['symbol'])}{name_suffix} added to Positions — "
        f"entry ${atr_info['current_price']:,.2f}, "
        f"ATR ({st.session_state.timeframe_label}) {atr_info['atr_value']:.2f}"
    )
    st.session_state.pop(_symbol_key(), None)
    st.session_state.add_form_epoch = st.session_state.get("add_form_epoch", 0) + 1
    # The disambiguation radio's key carries the searched symbol, so there is
    # one per symbol the user has looked at this session -- drop them all rather
    # than leak a widget value per search.
    stale = [k for k in st.session_state if k.startswith("add_match_choice_")]
    for k in ("symbol_check", "pending_symbol_check", *stale):
        st.session_state.pop(k, None)
    _clear_grid_selection()
    save_state()


def run_pending_symbol_check(symbol: str, picked_class: str) -> None:
    """Live-verify `symbol` on TradingView across every asset class, so the user
    never has to pick Crypto/Stock/Commodity manually. Falls back to a name
    search (e.g. "Apple" -> AAPL) when the literal text doesn't resolve as a
    ticker on any exchange. Stores errors alongside matches so the UI can tell
    a genuine "not listed anywhere" result apart from a TradingView-side
    failure (rate limiting, upstream outage) instead of showing the same
    "not found" message for both."""
    tf = TIMEFRAME_OPTIONS[st.session_state.timeframe_label]
    classes = None if picked_class == "All" else [picked_class]
    # The classes the lookup ACTUALLY examined, for the "wasn't found as a ..."
    # message. Starts at what the fan-out leg would cover; each leg that runs
    # replaces it with what that leg really touched.
    classes_checked = classes or ASSET_CLASSES
    matches, errors = [], {}
    # Search first, literal-ticker fan-out only as a fallback. One search
    # round-trip names the exact exchange that lists the symbol, so verifying it
    # costs one fetch per candidate instead of 8-per-class of guessing -- which
    # is both the reason a coin is now findable on ANY exchange and the reason
    # this got faster rather than slower. It also collapses the old two-tier
    # "ticker, then name" split: "solana", "SOL" and "SOLUSD" all take the same
    # path, at the same cost.
    try:
        candidates = search_candidates(symbol, picked_class)
    except (requests.RequestException, ValueError) as exc:
        # The literal leg below distinguishes "doesn't exist" from "TradingView
        # is down" via error["retryable"]; this leg must do the same, or an
        # outage renders as the false claim "not found on any exchange".
        errors["Symbol search"] = {
            "message": f"TradingView symbol search failed ({exc}).",
            "retryable": True,
        }
        candidates = []
    shortlist = shortlist_candidates(candidates)
    if shortlist:
        # Never restricted to the pre-picked class: a name or ticker's real
        # asset class is only known once it resolves, so "Microsoft" has to stay
        # matchable while "Crypto" is selected.
        #
        # But report the classes the shortlist actually COVERS, not all three.
        # Hard-coding ASSET_CLASSES here made the failure message claim work it
        # never did: "bravo" with Crypto picked surfaced only Stock candidates,
        # and still reported "wasn't found as a Crypto, Stock, or Commodity
        # symbol" -- Commodity was never looked at.
        classes_checked = [ac for ac in ASSET_CLASSES
                           if any(c["asset_class"] == ac for c in shortlist)]
        matches, search_errors = verify_search_candidates(shortlist, tf)
        errors = {**errors, **search_errors}
    # Skip the fan-out when the search leg already hit a retryable upstream
    # failure: nothing it learned says the symbol doesn't exist, and 24 more
    # blind fetches against an endpoint that is currently rate-limiting us just
    # buys a slower "Retry" (measured: 28-43s per query during an upstream
    # wobble, against ~1.3s healthy). The user gets the Retry path instead.
    upstream_wobble = any(err.get("retryable") for err in errors.values())
    if not matches and not upstream_wobble:
        # Safety net for anything TradingView's search doesn't surface but
        # analyze_coin can still price: the original guess-the-exchange fan-out,
        # unchanged. Keep BOTH error sets -- if that leg's emptiness was itself
        # a retryable failure, discarding the search leg's errors would hide the
        # Retry path and show a definitive "not found" instead.
        literal_matches, literal_errors = detect_asset_classes(symbol, tf, classes)
        # Both legs ran, so the message must report their UNION -- overwriting
        # would drop the classes the search leg already ruled out.
        fanned_out = classes or ASSET_CLASSES
        classes_checked = [ac for ac in ASSET_CLASSES
                           if ac in classes_checked or ac in fanned_out]
        if literal_matches:
            matches = literal_matches
            errors = literal_errors
        else:
            errors = {**errors, **literal_errors}
    # A concrete Asset Class pick filters the ANSWER, not just the ranking. The
    # SEARCH leg still examines every class -- picked_class is only a sort key
    # in rank_search_candidates, never a filter -- which is what keeps
    # "Microsoft" reachable while Crypto is selected. (The literal fan-out
    # fallback above is class-restricted; this only bites on the search leg.)
    # Once the picked class has produced a match of its own, anything outside it
    # is noise: "btc" under Crypto was offering BTCUSD and BTC Health Ltd
    # (Stock, ASX) side by side and asking which was meant. The `if in_class:`
    # guard is what stops this from ever narrowing to zero, so nothing that used
    # to be findable stops being findable.
    if picked_class != "All":
        in_class = [m for m in matches if m["asset_class"] == picked_class]
        if in_class:
            matches = in_class
    if len(matches) > 1:
        # Keep the old one-best-per-asset-class set, PLUS every additional asset
        # the typed text actually names -- strictly additive, so no cross-class
        # prompt that used to appear can disappear here. The extras are what
        # makes "blackrock" offer BLK, TCPC and BRC; the name-match gate is what
        # stops "solana" from offering Solv Protocol and Solayer alongside SOL,
        # since TradingView's search is fuzzy and returns them too. Matches from
        # the literal fan-out fallback carry no search metadata, so they are
        # never "named" and collapse to one per class exactly as before.
        kept, seen_classes = [], set()
        for m in matches:                       # already in rank order
            if m["asset_class"] not in seen_classes or m.get("named_match"):
                kept.append(m)
                seen_classes.add(m["asset_class"])
        matches = kept[:_MAX_MATCH_CHOICES]
    if matches:
        # The search hit already carries TradingView's description for the exact
        # listing that matched, so prefer it over a second round-trip for the
        # same string. With six BlackRock companies on screen that fan-out was
        # six extra searches, and it alone pushed the lookup past the 2s budget.
        # Only the literal fan-out fallback, which never saw a search hit, still
        # has to ask.
        for match in matches:
            match["display_name"] = match.get("description") or None
        unnamed = [i for i, m in enumerate(matches) if not m["display_name"]]
        if unnamed:
            with ThreadPoolExecutor(max_workers=len(unnamed)) as pool:
                futures = {i: pool.submit(lookup_display_name, matches[i]["symbol"], matches[i]["exchange"])
                           for i in unnamed}
                for i, fut in futures.items():
                    matches[i]["display_name"] = fut.result()
    st.session_state.symbol_check = {
        "symbol": symbol,
        "matches": matches,
        "errors": errors,
        "classes_checked": classes_checked,
    }


def match_choice_label(match: dict) -> str:
    """One row of the disambiguation prompt: ticker, company/coin name, then
    where it trades. The name is the only thing that tells BlackRock, Inc.
    apart from BlackRock TCP Capital Corp. -- the tickers (BLK, TCPC) do not.

    display_name and description are both TradingView's own text, so both go
    through _md_escape: st.radio renders option labels as markdown, and a
    description containing * or _ would otherwise restyle the row."""
    name = match.get("display_name") or match.get("description") or ""
    named = f" — {_md_escape(name)}" if name else ""
    return (f"**{_md_escape(match['symbol'])}**{named}  ·  "
            f"{match['asset_class']} · {_md_escape(match['exchange'])}")


def max_safe_atr_multiple(entry_price: float, atr_value: float) -> float | None:
    """Largest selectable ATR multiple that still leaves the stop above zero.

    The stop is entry - multiple*ATR, so it reaches zero at exactly
    entry/ATR. That break-even is almost never a round number, so floor it to
    the widget's step: suggesting 4.3 when the true limit is 4.32 is safe,
    suggesting it when the limit is 4.28 is not. Steps down once more when the
    break-even lands exactly on the grid, since a stop of exactly zero is
    still unplaceable. None means no usable multiple exists (ATR is at or
    above the entry price itself, which points at bad ATR data, not a setting
    the user can fix).
    """
    if atr_value <= 0 or entry_price <= 0:
        return None
    break_even = entry_price / atr_value
    steps = math.floor(break_even / ATR_MULTIPLE_STEP)
    safe = round(steps * ATR_MULTIPLE_STEP, 1)
    if safe >= break_even:
        safe = round(safe - ATR_MULTIPLE_STEP, 1)
    return safe if safe >= ATR_MULTIPLE_MIN else None


def compute_outputs(entry_price: float, atr_value: float, atr_multiple: float,
                     tranches: int, portfolio_size: float, risk_pct: float) -> dict | None:
    # Checked before the comparisons below, which cannot catch a NaN on their
    # own. fetch_atr now rejects non-finite values at the network boundary, but
    # a positions.json written before that guard existed (or edited by hand)
    # can still hold one, so a poisoned row falls into the existing
    # "can't compute this row" path instead of crashing the render.
    if not all(_finite_number(v) for v in
               (entry_price, atr_value, atr_multiple, tranches, portfolio_size, risk_pct)):
        return None
    if entry_price <= 0 or tranches <= 0 or portfolio_size <= 0:
        return None
    r_dollars = portfolio_size * (risk_pct / 100.0)
    stop_distance_dollars = atr_value * atr_multiple
    stop_distance_pct = stop_distance_dollars / entry_price
    if stop_distance_pct <= 0:
        return None
    stop_price = entry_price - stop_distance_dollars
    position_size_dollars = r_dollars / stop_distance_pct
    position_size_pct = position_size_dollars / portfolio_size
    return {
        # A stop at or below zero is unplaceable, so the position has no
        # reachable stop and every size below it is derived from a premise that
        # can't be executed. Reported as a flag rather than a None return so
        # the caller can keep the diagnostic columns (ATR, stop distance) on
        # screen and name the multiple that would fix it. Equivalent to
        # stop_distance_pct >= 1, i.e. a stop a full 100% below entry.
        "stop_reachable": stop_price > 0,
        "r_dollars": r_dollars,
        "stop_distance_dollars": stop_distance_dollars,
        "stop_distance_pct": stop_distance_pct,
        "stop_price": stop_price,
        "position_size_dollars": position_size_dollars,
        "position_size_pct": position_size_pct,
        "tranche_size_dollars": position_size_dollars / tranches,
        "tranche_size_pct": position_size_pct / tranches,
    }


st.set_page_config(page_title="Position Size Calculator", page_icon="💲", layout="wide")


def queue_toast(message, icon="⚠️"):
    # st.rerun() abandons the current run's UI deltas, so a toast fired in the
    # same run as a rerun (every grid-edit rejection path) silently never
    # renders. Queue it in session state instead; the drain below shows it on
    # the rerun's own run.
    st.session_state.setdefault("deferred_toasts", []).append((message, icon))


for _msg, _icon in st.session_state.pop("deferred_toasts", []):
    st.toast(_msg, icon=_icon)

# Streamlit's own header bar is position:absolute at the very top (60px tall,
# opaque white, z-index 999990) rather than taking up flow space, so the main
# content's default 96px top padding existed to clear it -- anything higher
# rendered underneath an opaque bar. Its only contents are the "Deploy" button
# and the ⋮ menu, both removed below, so that constraint is gone and the top
# padding can come down to what the design actually wants. Same story in the
# sidebar: stSidebarHeader reserves 60px for the collapse toggle, far more than
# the 28px control needs.
st.markdown(
    """
    <style>
    /* Streamlit's "Deploy" button and ⋮ menu. Neither belongs in a calculator
       run from a local folder: Deploy advertises Streamlit Community Cloud,
       and the menu's "Clear cache" would silently throw away the price/ATR
       cache mid-session.
       These two are hidden INDIVIDUALLY and stToolbar is left displayed on
       purpose. Hiding the toolbar itself also hid stExpandSidebarButton, which
       Streamlit renders *inside* it -- and that is the only control that brings
       a collapsed sidebar back, so the app became unusable with no way to reach
       Portfolio Size again. Never hide stToolbar or stHeader wholesale. */
    [data-testid="stAppDeployButton"],
    [data-testid="stMainMenu"] {
        display: none !important;
    }
    /* The toolbar centres its children on the header's vertical midpoint, and
       the header is collapsed to 0 below -- which put the expand-sidebar button
       at top:-14, half of it clipped above the viewport edge. Align to the top
       of that zero line and offset it deliberately instead, so the button sits
       fully on screen and above the title (which starts at 38px). */
    [data-testid="stToolbar"] {
        align-items: flex-start !important;
        padding-top: 8px !important;
    }
    /* With Deploy and the menu gone the header is an empty opaque 60px bar
       pinned over the top of the page -- exactly what the padding below had to
       clear. Collapsed to 0 with overflow left visible so the expand-sidebar
       button inside it still paints and stays clickable; display:none here
       would take that button out with it. */
    [data-testid="stHeader"] {
        height: 0 !important;
        min-height: 0 !important;
        background: transparent !important;
        overflow: visible !important;
    }
    /* No longer floored by the 60px header: the title now sits where the page
       wants it rather than below a bar that is no longer drawn. */
    [data-testid="stMainBlockContainer"] {
        /* Paired with the sidebar offset below to sit the title and "Global
           settings" on one line, clear of the top edge. Also leaves room for
           the expand-sidebar button, which Streamlit draws at the very top-left
           once the sidebar is collapsed. */
        padding-top: 2.5rem !important;
        padding-left: 1.5rem !important;
    }
    /* Every st.markdown("<style>...") gets wrapped in an element container that
       is 0px tall but still a flex item of the block it sits in -- so each one
       silently claims a row-gap (16px here). The three style blocks above the
       title were adding 48px of empty space that no amount of container padding
       could reclaim, because it was BETWEEN siblings, not inside the parent.
       display:none, not height:0: only display:none takes an item out of flex
       layout and takes its gap with it. A <style> element's rules apply from
       being in the document, so hiding its wrapper costs nothing.
       Two separate :has() clauses rather than one `>` chain. The first pins
       this to a LEAF markdown element -- a column's or container's element
       container holds an stVerticalBlock, never a direct stMarkdown child, so
       it can never match and have its real content hidden. The second tests the
       payload by descendant, because Streamlit puts an unnamed emotion-cache
       div between stMarkdown and stMarkdownContainer whose presence varies by
       version; a strict chain through it silently stops matching on an upgrade.
       style:only-child is what makes it safe: a markdown that renders text AND
       carries a style tag has two children and is left alone. */
    [data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"]):has([data-testid="stMarkdownContainer"] > style:only-child) {
        display: none !important;
    }
    /* Taken out of flow rather than shrunk. This header exists only to hold the
       collapse toggle, and absolute positioning hands its 34px back to the
       sidebar content while leaving the toggle exactly where it was -- which is
       what lets "Global settings" rise onto the title's line. Safe because the
       heading's text ends at x=146 and the toggle starts at x=154, so they
       share the line without touching (a longer heading would collide). No
       z-index needed: a positioned element paints above static siblings, so the
       toggle stays visible and clickable over the heading's box. */
    [data-testid="stSidebarHeader"] {
        position: absolute !important;
        top: 0 !important;
        left: 20px !important;
        right: 20px !important;
        height: 34px !important;
        min-height: 34px !important;
        padding-top: 0.25rem !important;
        margin-bottom: 0 !important;
    }
    /* Keep the collapse "«" on screen at all times. Streamlit rests it at
       visibility:hidden and only reveals it while the pointer is over the
       sidebar, so the single control that hides the panel cannot be found
       without already knowing where to aim -- and it is the mirror of the "»"
       expand button, which Streamlit paints unconditionally.
       It is `visibility`, not `opacity`: an opacity override matches this
       element and changes nothing on screen (measured live, both states).
       The wrapper alone is enough because visibility inherits -- the inner
       <button> and the icon carry no hidden declaration of their own, they were
       only inheriting this one, so naming them too would be dead selector.
       Deliberately NOT applied to stExpandSidebarButton: that one already rests
       at visibility:visible/opacity:1, so a rule there would be a no-op.
       Side effect, and a wanted one: a visibility:hidden button is not
       focusable, so this also puts the collapse toggle back in the keyboard tab
       order alongside the other real controls. */
    [data-testid="stSidebarCollapseButton"] {
        visibility: visible !important;
    }
    /* Aligns the SIDEBAR HEADING'S TEXT with the title's text, not their boxes:
       this h2 carries 16px of its own padding-top and a 20px font against the
       h1's 0 padding and 28px font, so equal box offsets would leave the two
       visibly ~14px apart. 29px here puts the h2's line-box centre at 56.75
       against the title's 56.5 -- re-measure both if either font size or the
       main padding above changes. */
    [data-testid="stSidebarUserContent"] {
        padding-top: 29px !important;
    }
    /* Scoped to the expanded state: forcing the width unconditionally keeps
       the collapsed sidebar's width reserved in the layout, which pushes the
       whole page right by a dead strip on tablet/mobile widths. */
    [data-testid="stSidebar"][aria-expanded="true"] {
        /* 236px, up from 206px, and the driver is the Refresh button: at the
           1rem label size every button now shares, "Refresh Price & ATR"
           measures 152px, and a 206px sidebar left only 129px inside that
           button's padding. Widening the panel is what keeps that label on
           ONE line without breaking the shared right edge below. */
        min-width: 236px !important;
        max-width: 236px !important;
    }
    /* One authoritative width for every control in this sidebar, so Portfolio
       Size, Risk (%) and the Refresh button share a right edge instead of each
       sizing itself. 186px is set by the longest button label at the 1rem size
       every button now shares: "Refresh Price & ATR" needs 152px plus that
       button's 24px of horizontal padding and its borders, so anything under
       ~178px puts it on two lines. 10px of slack above that, and 10px short of
       the 196px content column, so the three controls sit inboard of the panel
       edge rather than flush against it.
       Whether Streamlit creates its own commit hint inside these inputs turns
       on their width, so re-check the sidebar commit-hint rule if this grows:
       at 186px (a 184px Portfolio Size field) it still creates none, measured.
       Consumed by the input widths, the button width, and the refresh "?"
       offset further down -- change it here only. A label longer than this
       wraps rather than silently breaking the alignment. */
    [data-testid="stSidebar"] {
        --pf-control-w: 186px;
    }
    /* Explicit sidebar grey: Streamlit's default #f0f2f6 barely separates from
       the white main area (1.121:1), so the sidebar does not read as a distinct
       panel. #e5e9f0 is 1.218:1 -- #dee2eb was tried first and rejected as too
       dark. Deliberately NOT applied to
       [data-testid="stApp"], which is the single element the whole page's white
       comes from (the view container, main block and header are all
       transparent) -- painting that grey would dissolve the main-area fields,
       which sit at rgb(240,242,246) with borders in the same grey. The
       sidebar's own fields stay white because BaseWeb paints the wrapper, not
       the input (measured: stNumberInputContainer is white, the input itself
       transparent). Body text #31333F on this measures 10.3:1, well clear of
       WCAG AA. Anything else painted to blend with this surface has to track
       it -- see the focus-hint chip further down. */
    [data-testid="stSidebar"] {
        background-color: #e5e9f0;
    }
    /* Sidebar vertical rhythm. Streamlit's 16px block gap was tuned when every
       control in here carried its own "?" tooltip trigger beside its label;
       those are all consolidated into the title's single help panel now, so
       the same 16px reads as slack -- eight elements were each paying it.
       Direct child ONLY, for two reasons: nested vertical blocks in here (the
       refresh spinner slot) set their own gap and must keep it, and this
       selector is built from attribute selectors, so as a descendant selector
       it would outrank .st-key-refresh_status's own `gap: 0` and silently
       reopen the 16px shift that rule exists to remove.
       Nothing opts out of this any more: the focus-hint chip that used to
       force a taller gap under Portfolio Size and Risk (%) now sits in the
       label row instead of below the field, so all eight gaps in here are the
       same 8px. */
    [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {
        gap: 8px !important;
    }
    /* The refresh spinner's slot, held open whether or not it is spinning (see
       the refresh_status container for the shift this prevents). 20px is the
       spinner's measured height; the negative margin absorbs the second 16px
       flex gap that adding an element here would otherwise open up, so the
       idle cost is the slot itself and nothing more. */
    .st-key-refresh_status {
        min-height: 20px;
        margin-bottom: -16px !important;
        /* The slot holds two children -- the zero-height anchor and, while a
           refresh runs, the spinner -- and Streamlit's block gap between them
           grew the slot 20px -> 36px, which put a 16px version of the very
           shift this exists to remove back on screen. */
        gap: 0 !important;
    }
    /* Hide the anchor/deeplink icons Streamlit adds next to every heading.
       Scoped to the <a> only: the same container also hosts a heading's
       help "?" icon (st.title(help=...)), which must stay visible. */
    [data-testid="stHeaderActionElements"] a {
        display: none !important;
    }
    [data-testid="stColumn"]:has([data-testid="stDownloadButton"]) {
        display: flex;
        justify-content: flex-end;
    }
    [data-testid="stColumn"]:has([data-testid="stDownloadButton"]) [data-testid="stVerticalBlock"] {
        align-items: flex-end;
    }
    [data-testid="stDownloadButton"] button {
        /* 2rem, not the 1.75rem this was: at the shared 1rem label size the
           text's own line box is 25.6px, which a 28px button clipped once its
           2px of border is counted. */
        height: 2rem !important;
        padding: 0 0.5rem !important;
        min-height: 0 !important;
    }
    /* Add-position row resize behavior. The proportional column ratios alone
       break at narrow windows: the button columns refuse to shrink (the
       fit-content rule above) and the spacer keeps claiming its ~35% share,
       so the FIELD columns absorb all the squeeze -- labels wrap vertically
       ("ATR mul tipl e") and inputs collapse to slivers. Give each field a
       usable floor, let the spacer collapse to nothing first, and let the
       row wrap (stHorizontalBlock is flex-wrap: wrap) once even that isn't
       enough. !important throughout: Streamlit sets its own min-width on every
       column. Scoped to the one row that contains the Search button. */
    /* Every field column's flex-basis equals its own min-width, deliberately.
       Flexbox wraps ONE item at a time and decides using each item's basis
       clamped by min-width, so mismatched bases give the four fields four
       staggered breakpoints and, in the window between them, drop a single
       field onto a line of its own. Started at 220/130 and "# Tranches"
       orphaned at ~950px of viewport; 200/130 just moved that to ~850px.
       Matching basis to min-width collapses all four onto ONE breakpoint --
       175+115+128+128 plus the three 8px gaps = 570px of row -- above which
       the four stay together and the two buttons wrap as a pair, which is the
       intended fallback. flex-grow still decides who gets the slack when there
       IS room, so wide layouts are unchanged. Below 570px one field does still
       wrap alone; closing that needs the fields nested in their own container,
       which is a layout change, not a width tweak. */
    [data-testid="stHorizontalBlock"]:has(.st-key-search_btn)
        [data-testid="stColumn"]:has(input[aria-label="Symbol, Name"]) {
        flex: 2 1 175px !important;
        min-width: 175px !important;
        max-width: 420px !important;
    }
    [data-testid="stHorizontalBlock"]:has(.st-key-search_btn)
        [data-testid="stColumn"]:has([data-testid="stSelectbox"]) {
        flex: 1 1 115px !important;
        min-width: 115px !important;
        max-width: 220px !important;
    }
    /* min-width here is load-bearing, not cosmetic: Streamlit REMOVES a
       number_input's -/+ steppers from the DOM (it does not hide them) once the
       field gets too narrow, so no CSS override restores them -- a column that
       shrinks past that line silently loses both buttons. Measured on
       streamlit==1.62.0: absent at 111px, present at 124px, so the cliff is
       somewhere between and anything under 124px is unsafe. Re-measure after a
       Streamlit upgrade by narrowing the window and checking whether
       [data-testid="stNumberInputStepUp"] is still in the DOM. 95px and the
       110px basis both sat under it, which is why the steppers vanished at
       mid-range window widths -- wide enough that the row had not wrapped yet,
       narrow enough that these two columns were squeezed to their floor. 128px
       clears 124px with headroom and stays under the 180px cap. */
    [data-testid="stHorizontalBlock"]:has(.st-key-search_btn)
        [data-testid="stColumn"]:has(input[aria-label="ATR multiple"]),
    [data-testid="stHorizontalBlock"]:has(.st-key-search_btn)
        [data-testid="stColumn"]:has(input[aria-label="# Tranches"]) {
        flex: 1 1 128px !important;
        min-width: 128px !important;
        max-width: 180px !important;
    }
    /* The empty spacer column: first to give up its width. :not(:has(...))
       keeps this off the field/button columns in the same row. */
    [data-testid="stHorizontalBlock"]:has(.st-key-search_btn)
        [data-testid="stColumn"]:not(:has(input, button, [data-testid="stSelectbox"])) {
        flex: 1 1 0px !important;
        min-width: 0 !important;
    }
    [data-testid="stDownloadButton"] button {
        min-width: fit-content !important;
    }
    /* No font-size here on purpose: this label takes the shared 1rem from the
       button rule below, like every other button in the app. The WEIGHT is
       overridden back to normal: the other three buttons are the actions that
       drive the flow (search, add, refresh), whereas this one only exports
       what is already on screen, and at 600 it was competing with them.
       (0,1,2) here beats the shared rule's (0,1,1). */
    [data-testid="stDownloadButton"] button p {
        font-weight: 400 !important;
        white-space: nowrap;
    }
    /* Buttons live in narrow proportional columns (e.g. st.columns([1,1,9]));
       shrinking the window otherwise wraps their labels mid-word ("Searc/h").
       Let a button column size to its content and keep labels on one line.
       Search and Add position are excluded: they are the last columns on the
       Add-position row, and content-sizing them left the row ending in dead
       space. Excluding them here (rather than overriding this rule afterwards)
       is what keeps Streamlit's own flex-basis, which is the only thing that
       carries the column weights -- the `flex` shorthand below would replace
       that basis with `auto` and hand the leftover width to the field columns
       instead. Their columns are far wider than the labels, so nothing wraps. */
    [data-testid="stColumn"]:has(.stButton):not(:has(.st-key-search_btn)):not(:has(.st-key-add_position_btn)) {
        flex: 0 0 auto !important;
        min-width: fit-content !important;
    }
    .stButton button p {
        white-space: nowrap;
    }
    /* ONE look for every labelled button in the app -- Search, Add Position,
       Refresh Price & ATR and the CSV download. This replaces three
       near-identical copies of the same palette (Search/Add here, the sidebar
       Refresh button in its own block, and the download button's default
       Streamlit grey), so the four can no longer drift apart.
       Size: Streamlit labels buttons at 14px inside a button whose own
       font-size is 16px, so every action in the app was labelled a step
       SMALLER than the body text and the sidebar's inputs.
       Palette: the sidebar's light blue, which was already on three of the
       four; the CSV button's default grey was the odd one out.
       Scoped to the two BaseButton variants that carry a text label. The
       steppers, the sidebar collapse chevron and the title's help icon are all
       untouched -- different testids, and their glyphs are SVG, not a <p>. */
    [data-testid="stBaseButton-secondary"],
    [data-testid="stBaseButton-primary"] {
        background-color: #cfe8fc !important;
        border-color: #a8d8f5 !important;
        color: #0b3d5c !important;
    }
    [data-testid="stBaseButton-secondary"] p,
    [data-testid="stBaseButton-primary"] p {
        font-size: 1rem !important;
        font-weight: 600 !important;
    }
    [data-testid="stBaseButton-secondary"]:hover:not(:disabled),
    [data-testid="stBaseButton-primary"]:hover:not(:disabled) {
        background-color: #b8ddf7 !important;
        border-color: #8bc9ef !important;
        color: #0b3d5c !important;
    }
    /* Every button, not just Add Position: these sit side by side, so a
       disabled Search keeping the full-strength blue while a disabled Add
       Position faded made two identically-dead buttons look like one live and
       one dead. */
    [data-testid="stBaseButton-secondary"]:disabled,
    [data-testid="stBaseButton-primary"]:disabled {
        background-color: #e8f3fb !important;
        border-color: #d3e9f8 !important;
        color: #7ba3bd !important;
    }
    /* Streamlit styles a stepper's hover and focus states with ONE declaration
       block (`:hover:enabled, :focus:enabled { color:#fff; background:#5DADE2 }`),
       so a clicked -/+ keeps the full highlight after the pointer leaves it.
       Move to the other half of the pair and both light up at once, which reads
       as two buttons held down.
       Only :focus is neutralised; :not(:hover) hands the button straight back to
       Streamlit's rule the moment the pointer is actually over it, so hover
       feedback is untouched. `:not(:focus-visible)` looks like the tidier
       discriminator and does nothing at all here -- clicking a stepper reruns
       the script, and the re-mounted button has its focus restored in script,
       which sets focus-visible true. Measured, not assumed.
       Dropping the focus cue costs no accessibility: these buttons are
       tabindex="-1", unreachable by keyboard, and a keyboard user steps the
       value with the arrow keys on the field itself.
       Both declarations have to be reverted together -- undoing the background
       alone leaves a white glyph on a transparent button, i.e. invisible. The
       literals are Streamlit's own resting values for this element. */
    [data-testid="stNumberInputStepDown"]:focus:not(:hover),
    [data-testid="stNumberInputStepUp"]:focus:not(:hover) {
        background-color: transparent !important;
        color: rgb(49, 51, 63) !important;
    }
    /* Search / Add position: one shared width so the pair reads as a unit.
       Colour, weight and label size all come from the shared button rule
       above -- this is the only thing that is theirs alone. */
    .st-key-search_btn button,
    .st-key-add_position_btn button {
        min-width: 9.5rem;
    }
    /* A CONSTANT floor under the symbol-check block, so the Positions table
       holds its place instead of being shoved down whenever a result, warning
       or confirmation appears. 56px is MEASURED against the green "found" bar
       as it renders with the halved padding set below: 50px for the one-line
       bar, which is what every width from 1100px up produces, so the floor
       clears it with 6px of slack and the bar renders inside the reservation
       instead of setting it.
       The two-line bar is 70px (measured at 950px and 850px, where the text
       wraps) and therefore OVERRUNS this floor by 14px, shoving Positions down
       at those widths. Deliberate, and the same trade the multi-match panel
       gets below: a 70px floor would buy stillness at narrow widths by parking
       20px of permanent blank space under the form at the widths actually used.
       This was 72px, measured before that padding was halved -- which left ~18px
       of permanent blank space under the form. Re-measure BOTH numbers together
       if the bar's padding or typography changes again.
       The multi-match disambiguation panel is taller again and is deliberately
       NOT covered: reserving its height would park that much blank space under
       the form every moment there is no result.
       Unconditional is the whole point -- reserving the height only while the
       block HAS content measured worse than reserving nothing at all, since a
       lookup passes through empty twice, so the space collapsed and sprang back
       mid-search. This never collapses. */
    .st-key-symbol_check_results {
        min-height: 56px;
        /* This container is itself a flex column with Streamlit's 16px gap, and
           it holds TWO children: the result bar plus a zero-height element
           container that is always present. A zero-height sibling still earns
           its gap, so the block measured bar + 16px and overshot the floor by
           exactly that -- moving Positions down 10px on every find. Nothing is
           ever visible between those two children, so the gap has no job here.
           This is what lets the floor above be the bar's own height. */
        gap: 0 !important;
        /* Streamlit's 16px flex gap between vertical siblings becomes 8px above
           and below this block: tightens Symbol row -> result bar and result
           bar -> Positions heading. Negative margin rather than a smaller
           floor, because the 56px above is what stops Positions moving when a
           result appears. Both values assume that 16px, which is Streamlit's
           theme gap -- an upgrade that changes it changes these gaps too. */
        margin-top: -8px;
        margin-bottom: -8px;
    }
    /* The result bar is a ONE-LINE confirmation, but Streamlit's alert padding
       (1rem block) sizes it like a paragraph. Halved on the block axis, and the
       inner <p> loses the margin Streamlit gives body copy. Scoped to this
       container so the st.info onboarding hints elsewhere keep their roomier
       padding -- they are real paragraphs. NOTE: the min-height above is the
       anti-jump reservation for exactly this bar, so it was re-measured
       against the shorter bar; changing this padding means re-measuring it. */
    .st-key-symbol_check_results [data-testid="stAlert"],
    .st-key-symbol_check_results [data-testid="stAlertContainer"] {
        padding-top: 0.4rem !important;
        padding-bottom: 0.4rem !important;
    }
    .st-key-symbol_check_results [data-testid="stAlert"] p {
        margin-bottom: 0 !important;
        line-height: 1.4 !important;
    }
    /* The OTHER half of that margin. Streamlit gives the alert's <p> a 1rem
       bottom margin and then CANCELS it with -16px on the markdown container
       wrapping it. Zeroing only the <p> above left that -16px uncompensated, so
       the alert's content box computed 16px SHORTER than the text inside it.
       At one line the alert's own padding absorbed the deficit and it looked
       right; at two lines the shortfall outran that slack and the second line
       rendered BELOW the green background -- visible once every text surface
       went to 16px and moved the wrap point into ordinary window widths.
       Both halves of the pair have to go, or neither. */
    .st-key-symbol_check_results [data-testid="stAlert"] [data-testid="stMarkdownContainer"] {
        margin-bottom: 0 !important;
    }
    /* Section headings ("Add position", "Positions") carry a 1rem top padding
       on top of Streamlit's own 16px block gap, which double-spaces every
       section break. The gap alone is enough separation. */
    [data-testid="stMainBlockContainer"] h3 {
        padding-top: 0 !important;
        padding-bottom: 0.25rem !important;
    }
    /* ONE text size for the whole app, and this is it. Streamlit ships four
       different sizes on surfaces that sit side by side: 16px body copy,
       15.2px captions, 14px widget labels AND input values, and 13px grid
       headers -- so the fields the user actually types into rendered a step
       SMALLER than the prose explaining them, and two steps smaller than the
       buttons next to them. Nothing about this app's content justifies four
       sizes.
       Everything non-bold is pinned to the 16px body size here. Hierarchy is
       carried by weight and colour instead: the bold surfaces (the four
       buttons, the sidebar's two driver inputs, the headings, the Position
       Risk metric) keep their own larger sizes and are listed in the rules
       that set them, not here.
       stTextInputField / stNumberInputField are the innermost field elements
       -- Streamlit declares the 14px directly on them, so an ancestor rule
       does not reach it. */
    [data-testid="stWidgetLabel"] p,
    [data-testid="stTextInputField"],
    [data-testid="stNumberInputField"],
    [data-testid="stCaptionContainer"] p,
    [data-baseweb="select"] div,
    /* The selectbox shows its value in a plain <input> carrying only emotion
       classes -- no testid, and not a div -- so neither of the rules above
       reaches it. Asset Class was the last 14px field on the page. */
    [data-testid="stSelectbox"] input,
    /* The last two 14px surfaces: st.metric's label ("Position Risk") and the
       radio options ("Hourly/Daily/Weekly"). Both are ordinary prose in a
       stMarkdownContainer, so only their own wrappers distinguish them.
       The sidebar's collapse chevron is deliberately not here -- its 24px is a
       Material icon glyph rendered as a font ligature, not text, and pinning
       it to 1rem would shrink the control rather than restyle a label. */
    [data-testid="stMetricLabel"] p,
    [data-testid="stRadioOption"] p {
        font-size: 1rem !important;
    }
    /* st.caption keeps its faded colour; only the size was the odd one out. */
    [data-testid="stCaptionContainer"] p {
        color: #444 !important;
    }
    /* MAIN-area captions only (the subtitle under the title): full black. The
       container itself carries opacity: 0.6, which grays out ANY color set on
       the inner <p> -- true black needs the opacity override too. The sidebar
       "Data refreshed as of" caption keeps the faded style above. */
    [data-testid="stMainBlockContainer"] [data-testid="stCaptionContainer"] {
        opacity: 1 !important;
    }
    [data-testid="stMainBlockContainer"] [data-testid="stCaptionContainer"] p {
        color: #000 !important;
    }
    /* Product title: compact, in the app's blue accent family (same palette as
       the Search/Add buttons) instead of the default 2.75rem black. #2E86C1
       (not the buttons' lighter #58a8e0) because the measured contrast of the
       light shade on white is 2.60:1 -- below even WCAG's 3:1 large-text
       minimum; this shade measures ~3.9:1 (accessibility review). */
    [data-testid="stMainBlockContainer"] h1 {
        font-size: 1.75rem !important;
        color: #2E86C1 !important;
        padding-top: 0 !important;
        padding-bottom: 0.25rem !important;
        /* The app's single help "?" is the h1's own action element, so making
           the heading a flex row is what lets it be pushed to the far right
           edge; by default it sits inline, immediately after the title text. */
        display: flex !important;
        align-items: center !important;
    }
    [data-testid="stMainBlockContainer"] h1 [data-testid="stHeaderActionElements"] {
        margin-left: auto;
    }
    /* Streamlit's help glyph already IS a ringed "?": its SVG is a stroked
       <circle r="10"> plus the "?" arc and its dot. So this only scales it up
       -- at Streamlit's 16px it reads as a footnote rather than as the app's
       one help affordance. No border and no border-radius here on purpose: an
       earlier version added `border: 2px solid` and drew a ring around the
       ring.
       #8e949f, not the lighter grey of the reference icon: a non-text control
       needs ~3:1 against its background and this measures 3.1:1 on white,
       where a #c8c8c8 ring measures 1.7:1 and disappears on a bright screen. */
    [data-testid="stMainBlockContainer"] h1 [data-testid="stTooltipHoverTarget"] button {
        display: flex !important;
        align-items: center;
        justify-content: center;
        width: 34px;
        height: 34px;
        padding: 0 !important;
        border: none !important;
        background: none !important;
        color: #8e949f;
        transition: color 120ms;
    }
    [data-testid="stMainBlockContainer"] h1 [data-testid="stTooltipHoverTarget"] button:hover {
        color: #2E86C1;
    }
    /* width/height are SVG *presentation attributes* (hard-coded 16), and CSS
       beats those, so both need overriding here rather than in markup. `fill`
       must stay none: the same precedence means a `fill: currentColor` here
       overrides the element's own fill="none" and floods the ring solid grey
       -- which is what this rule used to do. */
    [data-testid="stMainBlockContainer"] h1 [data-testid="stTooltipHoverTarget"] button svg {
        width: 26px !important;
        height: 26px !important;
        /* !important on stroke too: Streamlit's own emotion rule for this svg
           sets stroke: rgba(49,51,63,.6) and wins on specificity otherwise, so
           the icon ignored `color` above and never turned blue on hover. */
        stroke: currentColor !important;
        fill: none !important;
    }
    /* The consolidated help panel measures ~513px tall, and Streamlit caps
       tooltips at max-height: 300px with overflow-y: auto -- so 42% of it sat
       behind an internal scrollbar, which defeats the point of gathering all
       the help into one hover. Viewport-relative so it can never be taller
       than the screen; on a short window it correctly scrolls again. Global on
       purpose: every other tooltip left in this app is one line, so a ceiling
       they never reach cannot affect them. */
    [data-testid="stTooltipContent"] {
        max-height: min(78vh, 620px) !important;
        /* Streamlit's own rule is a flat `max-width: 672px` with no viewport
           term, so on a window narrower than that the panel keeps its 672px
           and simply runs off the right edge -- the last words of every line
           unreachable, with no horizontal scrollbar to get at them (the
           element's overflow is `auto`, but the clipping happens outside it,
           at the window). Harmless while every tooltip was one short line;
           this one is eight lines of prose and hit it immediately.
           min(), so the 672px ceiling still governs on a wide screen and this
           only ever shrinks the panel. 2rem of slack rather than 1rem because
           100vw counts the vertical scrollbar's width while the panel's
           containing block does not. */
        max-width: min(672px, calc(100vw - 2rem)) !important;
        /* Close the moment the pointer leaves the "?" itself. BaseWeb keeps a
           tooltip open while the pointer is over the tooltip BODY too, which is
           right for a one-line hint you might want to select, and wrong here:
           this panel is 672x476 and opens directly beneath the icon, so the
           natural move from the icon back to the page goes straight THROUGH
           it and the panel appears stuck open. Measured: leaving the icon
           upward (missing the panel) closes it immediately; leaving it
           downward (into the panel) does not.
           pointer-events: none makes the panel transparent to the mouse, so
           there is no mouseenter to cancel the icon's mouseleave and no path
           that keeps it alive. The cost is that the panel can no longer be
           scrolled or text-selected with the mouse -- it only ever scrolls on
           a window shorter than ~610px, where the max-height above bites. */
        pointer-events: none !important;
    }
    /* st.toast stacks fixed at the top-RIGHT corner (right:0, under the Deploy
       toolbar); pin the stack to the left edge instead. */
    [data-testid="stToastContainer"] {
        left: 0.5rem !important;
        right: auto !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Streamlit's number_input gives no font-size prop, and these two fields drive
# every downstream calculation, so bump their input text via aria-label — the
# same attribute Streamlit sets from the widget's label — to keep the change
# scoped to just these two fields instead of every number input on the page.
st.markdown(
    """
    <style>
    /* Size and weight for these two come from the sidebar block further down,
       which at (0,2,0) outranks this selector's (0,1,1) -- the 1.1rem/600 that
       used to sit here never applied and has been removed rather than left to
       read as the authority on it. Padding is this rule's alone. */
    input[aria-label="Portfolio Size ($)"],
    input[aria-label="Risk (%)"] {
        padding: 2px 8px !important;
    }
    div[data-testid="stTextInputRootElement"]:has(input[aria-label="Portfolio Size ($)"]),
    div[data-testid="stNumberInputContainer"]:has(input[aria-label="Risk (%)"]) {
        height: 30px !important;
        min-height: 30px !important;
        /* Applied to the input's root box, not its stElementContainer: the
           container also anchors the "Auto-applies as you pause" hint (its
           `top: 100%` resolves against it), and narrowing that would drag the
           hint in with it. */
        width: var(--pf-control-w) !important;
    }
    [data-testid="stElementContainer"]:has(input[aria-label="Portfolio Size ($)"]) [data-testid="stWidgetLabel"],
    [data-testid="stElementContainer"]:has(input[aria-label="Risk (%)"]) [data-testid="stWidgetLabel"] {
        margin-bottom: 0.1rem !important;
    }
    [data-testid="stElementContainer"]:has(input[aria-label="Portfolio Size ($)"]),
    [data-testid="stElementContainer"]:has(input[aria-label="Risk (%)"]) {
        position: relative;
    }
    /* No bottom reservation on these two fields any more, which is what lets
       them take the same 8px gap as everything else in the panel. The focus
       hint used to hang BELOW the field, so ~23px had to be reserved under
       each one (a 16px margin plus the gap) whether or not anyone was typing.
       The hint sits in the label row now -- see its rule below -- so it costs
       no vertical space at all. */
    /* Streamlit only renders its native commit hint on a wide enough widget;
       at 184px in the pinned 236px sidebar these inputs are still under
       whatever that threshold really is, so the element is never created (not
       merely hidden) and has to be recreated here. The width was 164px when
       this was written and the threshold was noted as "~180px" -- 184px
       produces no hint either, so treat that figure as a lower bound, not a
       measurement, and re-check by looking for InputInstructions in the
       sidebar if these fields are ever widened again. Shown while the field has focus, sitting in the natural 1rem gap
       below the field -- do NOT reintroduce a negative margin-bottom on these
       containers: -14px once collapsed that gap and the hint landed on top of
       the next widget's label. Anchored by `top`, not `bottom: -15px`: the
       box is line-height tall (0.7rem x 1.6 = 17.92px) but was only pushed
       15px down, so ~1.1px of it sat over the field's bottom border and its
       opaque background erased a notch out of the focus ring. `top: 100%`
       starts the box at the container's bottom edge, so it always grows
       downward and can never reach back into the field at any font size.
       Wording reflects the real behavior: both
       fields auto-commit 1.5s after typing pauses, so "Press Enter to apply"
       overstated what's required (UX review); shown on BOTH auto-apply
       fields, not just Portfolio Size. Screen readers get the same info via
       each field's help tooltip (a ::after on an ancestor never enters the
       input's accessible description). */
    [data-testid="stElementContainer"]:has(input[aria-label="Portfolio Size ($)"]):focus-within::after,
    [data-testid="stElementContainer"]:has(input[aria-label="Risk (%)"]):focus-within::after {
        /* "Auto-applies", not "Auto-applies as you pause": the full sentence
           does not fit beside the longer of the two labels in the row this
           now sits on. The behaviour it names is spelled out in the title's
           help panel under "Settings apply instantly" -- this is the
           at-the-field reminder, not the explanation. */
        content: "Auto-applies";
        position: absolute;
        /* Pinned to the LABEL ROW (top: 0, right-aligned into the width the
           label leaves) rather than hanging under the field at
           `top: calc(100% + 5px)`. Anchored below, it forced every gap under
           these two fields to reserve ~23px PERMANENTLY to host a hint that
           only appears while the field has focus -- dead space either side of
           Risk (%) at all times. Up here it overlaps nothing and reserves
           nothing, so the whole sidebar keeps one 8px rhythm.
           right/left rather than a width: the box shrink-wraps its text, so it
           stays clear of the label's own start. */
        right: 0;
        top: 0;
        line-height: 1.2;
        font-size: 0.65rem;
        /* Both values track the SIDEBAR background, not the field: the chip
           hangs below the input (top: calc(100% + 5px)) onto the panel behind
           it. It was rgb(240,242,246) on #808495 text, left over from when the
           sidebar was Streamlit's default -- against #e5e9f0 that measured
           1.086:1, a visible lighter patch, and the text only 3.3:1. #595e6b
           is the same value the Symbol placeholder below uses for the same
           reason, and gives 5.3:1 here. Update both if the sidebar changes. */
        color: #595e6b;
        background-color: #e5e9f0;
        padding: 0 3px;
        z-index: 10;
    }
    /* Symbol placeholder: default gray measured 3.54:1 on the field's
       rgb(240,242,246) background -- below WCAG 1.4.3's 4.5:1 (accessibility
       review). #595e6b is ~5.7:1 on the same background. */
    input[aria-label="Symbol, Name"]::placeholder {
        color: #595e6b !important;
        opacity: 1 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ATR multiple / # Tranches changes are picked up on the very next rerun
# regardless of how that rerun is triggered (Enter, blur, or clicking
# Search/Add position) -- so the native "Press Enter to apply" hint overstates
# what's actually required and is hidden here for just these two fields.
st.markdown(
    """
    <style>
    [data-testid="stElementContainer"]:has(input[aria-label="ATR multiple"]) [data-testid="InputInstructions"],
    [data-testid="stElementContainer"]:has(input[aria-label="# Tranches"]) [data-testid="InputInstructions"] {
        display: none !important;
    }
    /* Symbol is the one field where "Press Enter to apply" is TRUE -- Enter is
       what fires the lookup -- so it is hidden by state, not outright like the
       two above. Streamlit right-aligns the hint inside the input, and this
       field's placeholder is long enough to run underneath it, so the two
       collide in exactly one state: focused AND empty. :placeholder-shown is
       that state, and it is also the state where the hint has nothing to act
       on. It reappears on the first keystroke, when pressing Enter would
       actually do something and the short typed text cannot reach it. */
    [data-testid="stElementContainer"]:has(input[aria-label="Symbol, Name"]:placeholder-shown)
        [data-testid="InputInstructions"] {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Streamlit's own disconnect dialog says "Is Streamlit still running? ... just
# restart it in your terminal: streamlit run yourscript.py" -- a command these
# users never type, naming a file that does not exist here. It appears exactly
# when the Python process is gone, so nothing server-side can reach it: this
# runs while the app is alive and keeps working in the dead page afterwards,
# which is also why it must be a components.html iframe (st.markdown never
# executes <script>). Keyed on the <h2> text rather than a "done" marker, so a
# React re-render that restores Streamlit's own wording is simply rewritten
# again -- and so an unrelated st.dialog is never touched.
components.html(
    """
    <script>
    try {
        const W = window.parent;
        const D = W.document;

        // height=0 leaves a wrapper that still claims a slot in the page's flex
        // column, costing a measured 16px gap under the title. Streamlit 1.62
        // does not put that height in an HTML attribute, so a CSS rule keyed on
        // iframe[height="0"] matches nothing; the iframe finds its own wrapper
        // instead, which cannot go stale. Hiding it does not stop this script --
        // it has already run, and observers fire regardless of display.
        for (const f of D.querySelectorAll("iframe")) {
            if (f.contentWindow !== window) continue;
            const slot = f.closest('[data-testid="stElementContainer"]');
            if (slot) slot.style.display = "none";
            break;
        }

        // Only the file the reader can actually double-click. Naming both on a
        // machine we can identify would make them hunt for a file that is not
        // there; an unrecognised platform falls back to showing both.
        //
        // userAgentData.platform is the supported way to ask, and is unaffected
        // by Chrome freezing the UA string; it is absent in Safari and Firefox,
        // so both older signals stay as fallbacks. Windows is tested first
        // because "Macintosh" and "Windows" never co-occur, but a future
        // platform string that contains neither must land on the both-files
        // branch rather than being guessed at.
        // --- platform decision (extracted by tests/test_stopped_dialog.py) ---
        // Everything to the end marker is pure: it reads navigator and builds
        // strings, touching no DOM, which is what lets a test run it under
        // node. Keep it that way -- move DOM work below the marker.
        const nav = W.navigator;
        const plat = (nav.userAgentData && nav.userAgentData.platform)
            || nav.platform || "";
        const ua = nav.userAgent || "";
        const IS_WIN = /Win/i.test(plat) || /Windows|Win32|Win64/i.test(ua);
        const IS_MAC = !IS_WIN && (/Mac/i.test(plat) || /Mac/i.test(ua));
        // Android's user agent also says "Linux", and a phone has no start file
        // to run, so it must not take the Linux branch -- it belongs on the
        // fallback that lists all three.
        const IS_LINUX = !IS_WIN && !IS_MAC && !/Android/i.test(ua)
            && (/Linux/i.test(plat) || /X11/i.test(ua));
        // The bare file name alone does not say where to look, and this folder
        // has no single name to write down: a "Download ZIP" unpacks to
        // position-size-calculator-main, a clone gives position-size-calculator,
        // and a renamed folder gives a third answer -- so Python injects the
        // folder the app is actually running from. Relative on purpose: the full
        // path would carry the reader's user name for no benefit, since they got
        // to this folder themselves to start the app in the first place.
        //
        // The separator belongs to the file, not to the reader: on an
        // unrecognised platform both files are listed, and a backslash in the
        // Mac path (or a slash in the Windows one) would name a path that does
        // not exist on either machine.
        const FOLDER = """ + json.dumps(APP_DIR_NAME) + """;
        const WIN_FILE = FOLDER + "\\\\Start Calculator (Windows).bat";
        const MAC_FILE = FOLDER + "/Start Calculator (Mac).command";
        // A command to type, not a path to point at: the file name has spaces,
        // so an unquoted ./Start Calculator (Linux).sh is a shell error rather
        // than a start. Single-quoted here purely so the inner double quotes
        // need no escaping. The folder moves into the sentence instead, since a
        // terminal is already inside it by the time this is run.
        const LINUX_FILE = './"Start Calculator (Linux).sh"';
        const FILES = IS_WIN ? [WIN_FILE]
            : IS_MAC ? [MAC_FILE]
                : IS_LINUX ? [LINUX_FILE]
                    : [WIN_FILE, MAC_FILE, LINUX_FILE];
        // Naming the actual file manager keeps the instruction pointing at the
        // reader's computer rather than at this window, which is the one place
        // the file cannot be opened from.
        const FILE_BROWSER = IS_WIN ? "File Explorer"
            : IS_MAC ? "Finder" : "your file browser";
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

        // Nothing on a web page can launch a local program, and once the server
        // is gone there is no process left to ask -- so the start file genuinely
        // cannot be run from here. What this can do is notice the moment the
        // reader starts it themselves and come back on its own. Streamlit's own
        // retry loop gives up after a while; this one keeps waiting.
        const health = new W.URL("_stcore/health", W.location.href).href;
        const alive = function () {
            return W.fetch(health, {cache: "no-store"})
                .then(function (r) { return r.ok; })
                // A refused connection is the expected state here, not a fault:
                // it is exactly what "still stopped" looks like.
                .catch(function () { return false; });
        };
        const watch = function () {
            if (W.__pscStoppedPoll) return;
            W.__pscStoppedPoll = W.setInterval(function () {
                alive().then(function (up) { if (up) W.location.reload(); });
            }, 2000);
        };
        const unwatch = function () {
            if (!W.__pscStoppedPoll) return;
            W.clearInterval(W.__pscStoppedPoll);
            W.__pscStoppedPoll = null;
        };

        const rewrite = function (dialog) {
            const h2 = dialog.querySelector('h2[slot="title"]');
            if (!h2 || h2.textContent.trim() !== "Connection error") return;
            // Streamlit puts the message and the code block in the element
            // straight after the title. If that ever stops being true we leave
            // the dialog exactly as Streamlit built it rather than half-edit it.
            const body = h2.nextElementSibling;
            if (!body) return;

            h2.textContent = "The calculator has stopped";
            body.textContent = "";
            const say = function (text, muted) {
                const p = D.createElement("p");
                p.textContent = text;
                p.style.margin = "0 0 0.75rem";
                // Streamlit sizes dialog body copy for a paragraph of prose;
                // this dialog is two short lines around a file name, and at
                // that size they read as a warning rather than an instruction.
                p.style.fontSize = muted ? "0.8em" : "0.9em";
                if (muted) { p.style.opacity = "0.75"; }
                body.appendChild(p);
            };
            // One instruction and one reassurance, and nothing else. Earlier
            // drafts also explained why it had stopped, that the box below is a
            // real file rather than a button, and where the positions are
            // stored -- five paragraphs that buried the only sentence anyone
            // has to act on. Naming the file browser carries the "this lives on
            // your computer" point on its own.
            say(INSTRUCTION);
            for (const name of FILES) {
                const box = D.createElement("div");
                // A solid rounded panel in the app's own grey reads as a button,
                // and the one thing this must not invite is a click inside the
                // dialog: nothing on a web page can open a file on the reader's
                // machine, so that click does nothing and looks like a fault. The
                // page icon, dashed edge and monospace name make it look like
                // the file it is naming rather than something to press.
                box.textContent = "\\uD83D\\uDCC4  " + name;
                // Kept to one line: a path broken across two lines reads as two
                // things, and the break lands mid-word because the only wrap
                // opportunities in it are the separator and the spaces in the
                // file name.
                box.style.cssText = "background:#f7f8fa;border:1px dashed #c8ccd4;"
                    + "border-radius:8px;padding:0.6rem 0.8rem;margin:0 0 0.5rem;"
                    + "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"
                    + "monospace;font-weight:600;white-space:nowrap;"
                    + "overflow-x:auto;font-size:13px;cursor:default;";
                body.appendChild(box);
                // Shrink to fit rather than pick a size: the folder name is
                // whatever the reader called their folder, so the width this
                // has to survive is not knowable in advance -- only measurable,
                // and only once the box is in the document. The floor keeps it
                // readable; a name long enough to overflow even at 9px scrolls
                // instead, which still shows the whole path.
                for (let px = 12; px >= 9 && box.scrollWidth > box.clientWidth; px--) {
                    box.style.fontSize = px + "px";
                }
            }
            // Deliberately no reload button. The poller below notices the restart
            // within two seconds and reloads on its own, so a button could only
            // ever do what is already happening -- and while the app is still
            // stopped there is nothing for it to do at all, which is exactly what
            // it looked like from the outside. Hence the first half of this line:
            // without it, a page that sits there is indistinguishable from a
            // page that has given up. The second half answers the thing anyone
            // fears here before they ask it.
            say("This page comes back on its own once it starts. "
                + "Your positions are saved.", true);

            watch();
        };

        if (W.__pscStoppedObserver) W.__pscStoppedObserver.disconnect();
        let queued = false;
        const sweep = function () {
            queued = false;
            const dialogs = D.querySelectorAll('[data-testid="stDialog"]');
            // Streamlit reconnects on its own if the app returns quickly enough.
            // Stop polling when it does, or the next poll would reload a page
            // that has already recovered.
            if (!dialogs.length) unwatch();
            for (const d of dialogs) rewrite(d);
        };
        // Rewriting sets text the guard above no longer matches, so our own
        // writes end the cycle instead of retriggering it. Debounced into the
        // frame before paint, so the replacement is what first appears rather
        // than a flash of Streamlit's wording.
        W.__pscStoppedObserver = new W.MutationObserver(function () {
            if (queued) return;
            queued = true;
            W.requestAnimationFrame(sweep);
        });
        W.__pscStoppedObserver.observe(D.body, {childList: true, subtree: true});
        sweep();
    } catch (e) {
        // Nothing to sanity-check at install time -- the dialog this targets
        // does not exist until the server dies -- so a failure to reach the
        // parent document is the only signal available, and it must not be
        // swallowed by an invisible iframe.
        console.error("[stopped-app dialog] install failed", e);
    }
    </script>
    """,
    height=0,
)

# Must run before the first widget is created: adopting newer values means
# assigning to widget-backed keys (timeframe_label is one), which Streamlit
# rejects once that widget has been instantiated in the current run.
if st.session_state.pop("_reload_from_disk", False):
    for key, value in load_state().items():
        st.session_state[key] = value
    st.session_state.portfolio_size_text = _money_field(st.session_state.portfolio_size)
    st.session_state._saved_snapshot = _snapshot(_state_payload())

if "loaded" not in st.session_state:
    for key, value in load_state().items():
        st.session_state[key] = value
    st.session_state.portfolio_size_text = _money_field(st.session_state.portfolio_size)
    # Seed the Risk (%) restore value for _sync_risk_pct; a 0.0 already on disk
    # is itself the wipe artifact that guard exists to prevent, so fall back to
    # the default rather than seeding the guard with the broken value.
    st.session_state._last_risk_pct = st.session_state.risk_pct or DEFAULT_STATE["risk_pct"]
    # Portfolio Size is deliberately NOT seeded when there is no real value yet:
    # its ABSENCE is how _sync_portfolio_size distinguishes "never entered" (a
    # fresh install, where an empty field is correct) from "cleared a real
    # value" (the 2026-09-02 wipe, where the previous value must be restored).
    # Seeding a placeholder would collapse those two cases back together.
    if st.session_state.portfolio_size > 0:
        st.session_state._last_portfolio_size = st.session_state.portfolio_size
    st.session_state.loaded = True
    # Seed the baseline so an untouched session never writes. Without it the
    # first rerun finds no snapshot, counts as "changed", and saves -- which is
    # precisely the passive overwrite the check in save_state() exists to stop.
    st.session_state._saved_snapshot = _snapshot(_state_payload())

if st.session_state.get("load_error"):
    # Escaped because the message quotes repr()s of whatever was in the file.
    st.error(_md_escape(st.session_state.load_error))

# Every explanation in the app, in one place. This replaced nine per-field "?"
# tooltips plus a shorter version of this text, which had the same facts written
# two and three times over -- stop distance was on the title AND on ATR
# multiple, the timeframe re-fetch on the title AND on ATR Timeframe,
# Portfolio Size × Risk % on the title AND on Position Risk. Merged here, not
# concatenated.
#
# One surface renders this string -- the "?" beside the title -- which is the
# point of the consolidation: nothing to keep in step with anything else.
#
# Known limit, measured rather than assumed. Streamlit renders the icon as a
# <span data-testid="stTooltipHoverTarget"> carrying tabindex="-1", and opens
# the panel on pointer hover only: focusing the span and pressing Enter both
# open nothing (verified in the browser). So it is not a silent stop in the tab
# order -- Streamlit keeps it out of the order entirely -- but the help text has
# no keyboard or screen-reader path at all. `help=` hands over no hook to change
# that markup, so closing the gap needs a SECOND, keyboard-reachable surface
# (an expander, or st.popover), which is a design decision about the one-tooltip
# rule and not a rewording of this comment.
HELP_BODY = (
    "- **ATR (Average True Range, 14-period)** — TradingView's average of each "
    "bar's true trading range on the selected timeframe. Hourly, Daily and "
    "Weekly measure typical hour-, day-, or week-sized moves.\n"
    "- **ATR multiple** — how many ATRs below entry the stop goes. Bigger = a "
    "wider stop and a smaller position for the same risk.\n"
    "- **Stop distance** = ATR × ATR multiple. Stop price = entry − stop distance.\n"
    "- **Position Risk** — the dollar amount you lose if one trade hits its stop, "
    "and no more. Portfolio Size × Risk %.\n"
    "- **Position size** — set so a stop-out loses exactly your Position Risk, "
    "then split across # Tranches.\n"
    "- **Tranche** — one of the equal entries a position is split into. Tranche "
    "size = position size ÷ # Tranches.\n"
    "- **Asset Class** — limits the symbol search to one class. Pick All to "
    "detect it automatically.\n"
    "\n"
    "**Example**\n"
    # Every "$" below is escaped. Streamlit's markdown treats $...$ as inline
    # math, so the five plain dollar signs this line used to carry were PAIRED
    # off (1st-2nd, 3rd-4th) and rendered as two green monospace code spans --
    # which also swallowed the "$" of "$4.00" and "$833", so the example quietly
    # showed the wrong amounts. Escaped, they are literal and the whole line
    # renders as ordinary body text at body size instead of shrunken code.
    "\\$10,000 portfolio · 1% risk · Weekly ATR 2.00 · ATR multiple 2 → stop "
    "distance \\$4.00 → position size \\$2,500 → 3 tranches of \\$833 each. "
    "A stop-out costs you \\$100.\n"
    "\n"
    "**Adding a position**\n"
    "Type a ticker or a company/coin name and press Enter (or click Search) to "
    "verify it live on TradingView. The exchange is picked automatically — "
    "biggest venues first — within the selected Asset Class.\n"
    "\n"
    "**Live data**\n"
    "Price & ATR are pulled from TradingView when a position is added and on "
    "every refresh. The table shows the last refresh, not a live feed.\n"
    "\n"
    "**Settings apply instantly**\n"
    "Portfolio Size and Risk (%) re-compute every saved position about a second "
    "after you stop typing — no need to press Enter."
)

st.title("Position Size Calculator", help=HELP_BODY)
st.caption(
    "Auto-calculated ATR sets your stop price and position size — capping your "
    "loss on any trade at the risk % you set."
)

# ── Global settings ──────────────────────────────────────────────────────────
def _sync_portfolio_size() -> None:
    raw = st.session_state.portfolio_size_text.replace(",", "").replace("$", "").strip()
    # Absent until a real value has been committed even once (see the seeding in
    # the load block above). None therefore means a fresh install that has never
    # had a portfolio size, where an empty field is the correct resting state --
    # not the silent wipe the guards below exist to block.
    last = st.session_state.get("_last_portfolio_size")
    if not raw:
        # A cleared field now auto-commits ~1.5s after the user stops typing
        # (see the auto-apply iframe below) -- treating "" as $0 would silently
        # zero the portfolio and every computed size mid-retype (this wiped the
        # saved portfolio on 2026-09-02). Clearing is a transient editing
        # state, never a request for a $0 portfolio: keep the last value.
        if last is None:
            value = 0.0
        else:
            value = st.session_state.portfolio_size
            st.toast("Portfolio Size can not be empty.", icon="⚠️")
    else:
        try:
            value = float(raw)
            # float() happily parses "nan"/"inf", NaN <= 0 is False so the
            # compute_outputs guard never fires, and json.dumps writes a bare
            # NaN token that is not valid JSON for any other reader -- treat
            # non-finite exactly like a parse failure.
            if not math.isfinite(value):
                raise ValueError(raw)
        except ValueError:
            value = st.session_state.portfolio_size
            # The "kept" clause is dropped when there is nothing to keep: on a
            # fresh install the retained value is the 0 sentinel, and "kept $0"
            # would report a $0 portfolio as though the user had chosen it.
            st.toast(
                f"Couldn't parse '{_md_escape(raw)}' as a number"
                + (f" — kept ${_fmt_money(value)}" if value > 0 else ""),
                icon="⚠️",
            )
        else:
            if value <= 0:
                # compute_outputs returns None for portfolio_size <= 0, so a $0
                # (or negative) portfolio blanks Stop Price and every size
                # column on every row. Unlike the empty-field case above this
                # reached save_state(), so a reload came back still blank
                # instead of self-healing. Same silent-zeroing class the
                # Risk (%) guard blocks: keep the last real value.
                if last is None:
                    # Nothing to restore on a fresh install, so stay unset and
                    # let the field read empty rather than echoing a typed "0"
                    # back as if it were a real portfolio. Still says something:
                    # blanking every size column with no explanation is exactly
                    # the silent no-op this guard was written to avoid.
                    value = 0.0
                    st.toast("Enter your portfolio size to calculate position sizes.",
                             icon="ℹ️")
                else:
                    value = last
                    # "more than zero", not "more than $0": st.toast renders
                    # markdown, and a second $ in the string pairs with the first
                    # into a LaTeX span that eats both dollar signs (verified live).
                    st.toast(f"Portfolio size must be more than zero — kept ${_fmt_money(value)}", icon="⚠️")
    if value > 0:
        st.session_state._last_portfolio_size = value
    st.session_state.portfolio_size = value
    st.session_state.portfolio_size_text = _money_field(value)
    save_state()


def _sync_risk_pct() -> None:
    # Streamlit coerces an emptied number_input to its min_value (0.0) BEFORE
    # this callback runs, so an accidental clear + blur/Enter arrives here as a
    # plausible-looking 0.00 -- the same silent-zeroing class that wiped the
    # portfolio on 2026-09-02, and the auto-apply JS guard can only block the
    # auto-commit path, not native blur/Enter. A 0% risk sizes every position
    # to $0, so treat it as the emptied-field case: keep the last real value.
    if st.session_state.risk_pct == 0.0:
        st.session_state.risk_pct = st.session_state.get("_last_risk_pct", DEFAULT_STATE["risk_pct"])
        st.toast(f"Risk can't be 0% — kept {st.session_state.risk_pct:.2f}%", icon="⚠️")
    else:
        st.session_state._last_risk_pct = st.session_state.risk_pct
    save_state()


def _on_timeframe_change() -> None:
    # ATR values are timeframe-specific, so a timeframe switch makes every
    # saved position's stored ATR stale the moment it's clicked. Kick off the
    # same two-step request -> rerun -> execute refresh flow the Refresh
    # button uses (the refreshing block below the button does the actual
    # work under a spinner) instead of leaving old-timeframe numbers on
    # screen until a manual click -- the same auto-apply rule the editable
    # fields follow.
    save_state()
    _clear_grid_selection()
    if st.session_state.positions:
        st.session_state.refreshing = True

with st.sidebar:
    st.header("Global settings")
    # Default sizes are wildly inconsistent across widget types: text_input and
    # number_input render their value at 22.4px/600, while st.metric renders at
    # 36px/400 — same kind of "a dollar amount" content, three different looks.
    # Pin the inputs to one shared size/weight; Position Risk stays the biggest
    # number in the sidebar since it's the headline takeaway of this section --
    # but only just. At 26px it was the largest text anywhere in the sidebar,
    # outranking the "Global settings" heading above it, which made a derived
    # read-only figure look more important than the two inputs that determine
    # it. 20px is the heading's own size: still a clear step up from the 16px
    # inputs, with nothing in the sidebar above the heading.
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] [data-testid="stTextInputField"],
        [data-testid="stSidebar"] [data-testid="stNumberInputField"] {
            font-size: 16px !important;
            font-weight: 600 !important;
        }
        [data-testid="stSidebar"] [data-testid="stMetricValue"] {
            font-size: 20px !important;
            font-weight: 600 !important;
        }
        /* Colour, weight and label size come from the shared button rule in
           the main style block. All that is left here is the width pin, which
           gives this button a right edge shared with the two inputs above --
           it was content-sized once, which is what made it 11px narrower than
           the fields. */
        [data-testid="stSidebar"] [data-testid="stBaseButton-primary"] {
            width: var(--pf-control-w) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    # st.number_input has no thousands-separator support, so a text_input is
    # used instead and reformatted with commas on every change.
    st.text_input(
        "Portfolio Size ($)", key="portfolio_size_text", on_change=_sync_portfolio_size,
    )
    # Auto-apply edited values after the user pauses typing, without requiring
    # Enter, on the two fields that edit committed data: Portfolio Size and
    # Risk (%). ATR multiple / # Tranches are deliberately NOT auto-committed:
    # their committed value is picked up on the next rerun anyway (same reason
    # their native hint is hidden above), and a synthetic Enter fires their
    # reset_symbol_check callback -- which would wipe a freshly verified symbol
    # panel and disable "Add position" while the user is mid-add (found in
    # review). The Symbol field is likewise excluded: committing it fires a
    # live TradingView search, and a mid-typing pause would search half-typed
    # tickers. st.markdown never executes <script> tags (innerHTML), so this
    # must be a components.html iframe reaching into the parent page. The
    # listener delegates on the parent document rather than binding input nodes
    # -- reruns can remount widgets, which would orphan a direct listener after
    # the first commit. A synthetic Enter keydown/keypress goes through
    # Streamlit's own commit path (same as the user pressing Enter), so each
    # widget's on_change logic runs unchanged. Skipped if the field already
    # lost focus -- blur commits natively. The iframe removes its own wrapper
    # from layout (see the top of the script). Grid cell edits get the same rule
    # via onCellEditingStarted on the AgGrid below -- this listener can't see
    # into the grid's iframe.
    _AUTO_COMMIT_LABELS = ("Portfolio Size ($)", "Risk (%)")
    components.html(
        """
        <script>
        try {
            const W = window.parent;
            const FIELDS = "__FIELDS__";

            // Hide this helper's own wrapper -- same self-identifying pattern as
            // the disconnect-dialog helper further down, and for the same reason:
            // a zero-height iframe still leaves an stElementContainer that is a
            // full flex item, so it earns the sidebar gap on BOTH sides and cost
            // a measured 2 gaps of dead space between Portfolio Size and Risk (%).
            // This was meant to be a CSS rule keyed on iframe[height="0"], but
            // Streamlit 1.62 sizes the iframe with inline CSS and sets no height
            // ATTRIBUTE at all (measured: getAttribute("height") === null), so
            // that rule matched nothing and both gaps were being paid in full.
            // Matching on contentWindow identity instead cannot go stale the way
            // a testid or attribute selector can, and it touches only this
            // iframe -- a future VISIBLE iframe in the sidebar is unaffected.
            // Hiding it does not stop this script: it has already run, and the
            // listener below is installed on the PARENT document, not in here.
            for (const f of W.document.querySelectorAll("iframe")) {
                if (f.contentWindow !== window) continue;
                const slot = f.closest('[data-testid="stElementContainer"]');
                if (slot) slot.style.display = "none";
                break;
            }
            if (W.__pfCommitHandler) {
                W.document.removeEventListener("input", W.__pfCommitHandler, true);
            }
            W.__pfCommitHandler = function (e) {
                const t = e.target;
                if (!t || !t.matches || !t.matches(FIELDS)) return;
                clearTimeout(W.__pfCommitTimer);
                W.__pfCommitTimer = setTimeout(function () {
                    if (W.document.activeElement !== t) return;  // blur already committed
                    // Never auto-commit an emptied field: number_inputs commit "" as
                    // their min_value (verified live: clearing Risk (%) committed
                    // 0.00 and saved it) and the portfolio guard would still fire a
                    // needless toast. Clearing is a mid-retype state -- only an
                    // explicit Enter/blur may commit it.
                    if (!t.value.trim()) return;
                    for (const type of ["keydown", "keypress"]) {
                        t.dispatchEvent(new W.KeyboardEvent(type, {
                            key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true,
                        }));
                    }
                }, 1500);
            };
            W.document.addEventListener("input", W.__pfCommitHandler, true);
        } catch (e) {
            // This iframe is invisible (height 0): without this, an install
            // failure (renamed label, blocked parent access) would kill the
            // feature with no signal anywhere -- not even the console.
            console.error("[auto-commit] install failed", e);
        }
        </script>
        """.replace(
            "__FIELDS__",
            ", ".join(f"input[aria-label='{label}']" for label in _AUTO_COMMIT_LABELS),
        ),
        height=0,
    )
    st.number_input(
        "Risk (%)", min_value=0.0, max_value=100.0, step=0.1, format="%.2f",
        key="risk_pct", on_change=_sync_risk_pct,
    )
    # Directly under Risk (%), not at the foot of the sidebar: this is the
    # product of Portfolio Size x Risk (%) and nothing else, so it belongs
    # beside its two inputs where a changed percentage can be read straight
    # off it -- it used to sit below the refresh controls, five elements away
    # from the field that drives it.
    r_dollars_preview = st.session_state.portfolio_size * (st.session_state.risk_pct / 100.0)
    # _fmt_money, not ",.0f": at $50 x 1.8% the metric showed $1 for a true
    # $0.90 -- an 11% overstatement in the one number the whole tool exists to
    # control (UX review).
    # Reads session_state, not this widget's return value, so moving it above
    # the rest of the sidebar cannot change what it shows.
    st.metric("Position Risk", f"${_fmt_money(r_dollars_preview)}")
    st.radio(
        "ATR Timeframe", options=list(TIMEFRAME_OPTIONS.keys()),
        key="timeframe_label", on_change=_on_timeframe_change,
    )
    # Visible, not tooltip content: a consequence, not vocabulary. One click
    # re-pulls price & ATR for every saved position, and that is needed with a
    # hand on the control -- months later, with 20 positions loaded -- not in a
    # reference panel read once on day one.
    st.caption("Switching this re-fetches price & ATR for all saved positions.")
    if st.button(
        "🔄 Refresh Price & ATR",
        type="primary",
        disabled=not st.session_state.positions or st.session_state.get("refreshing", False),
        # Disabled-state reason only, which is why it can live on the button now:
        # the "ambush on the way to clicking" that once pushed this onto its own
        # "?" icon was a property of the documentation that used to be here (now
        # in the title's help), and a hint shown only while the control is dead
        # cannot ambush a click. Same shape as Search and Add Position.
        help=(
            "Refreshing..." if st.session_state.get("refreshing")
            else "Add a position first" if not st.session_state.positions
            else None
        ),
    ):
        # Two-step (request -> rerun -> execute) so the button actually renders
        # disabled while the refresh runs, instead of only disabling on the
        # NEXT click after the work already finished in this same script run.
        st.session_state.refreshing = True
        _clear_grid_selection()
        st.rerun()

    # A CONSTANT slot for the refresh spinner. st.spinner inserts a 20px element
    # here while the refresh runs, which pushed the timestamp caption below it
    # down 36px (the spinner plus the sidebar's 16px flex gap) and back up again
    # when the refresh finished -- so everything under the button jumped twice
    # per press. Reserving the slot unconditionally makes the busy and idle
    # layouts identical. The zero-height child is what keeps the container in
    # the DOM while idle: Streamlit prunes an empty container, and a min-height
    # on an element that isn't there reserves nothing.
    with st.container(key="refresh_status"):
        st.html("<div style='height:0'></div>")
        if st.session_state.get("refreshing"):
            # Empty label on purpose: just the spinner icon, no "Refreshing..." text.
            with st.spinner(""):
                refresh_all_positions()
            # The "Add position" verification below (asset class / exchange / live
            # price / ATR) is a snapshot from whenever the user last pressed Enter
            # in Symbol — a full ATR/price refresh makes that snapshot stale, so
            # clear it rather than leave old numbers displayed next to fresh ones.
            st.session_state.symbol_check = None
            st.session_state.pending_symbol_check = None
            st.session_state.refreshing = False
            st.rerun()
    if st.session_state.get("last_refreshed_at"):
        # Two-space markdown line break: in the 236px sidebar the one-line form
        # wraps mid-date ("2026-\n09-02"); breaking before the date keeps the
        # timestamp intact on its own line.
        st.caption(
            f"Data refreshed as of  \n{_md_escape(st.session_state.last_refreshed_at)}")
        # Visually-hidden live region: the caption above changes silently on
        # refresh, which screen readers never announce (WCAG 4.1.3 -- a11y
        # review). Kept separate so the visible caption's faded style stays.
        st.markdown(
            '<span role="status" aria-live="polite" style="position:absolute;'
            'width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);">'
            # Escaped: this is the one raw-HTML sink in the file that renders a
            # non-literal, and the value round-trips through positions.json, so
            # anything able to write that file could otherwise land script in
            # the page's own origin on every load.
            f"Data refreshed as of {html.escape(str(st.session_state.last_refreshed_at))}</span>",
            unsafe_allow_html=True,
        )

# ── Add position ─────────────────────────────────────────────────────────────
st.subheader("Add position")
# The greyed-out Search/Add pair needs an explanation that outlives a hover and
# a toast: a fresh install opens on exactly this state, and the control that
# unblocks it is in the sidebar, away from the form the user is looking at.
if not _portfolio_size_set():
    st.info(
        "Enter your **Portfolio Size** in the sidebar to search symbols — every "
        "position size is calculated as a share of it."
    )
# Fields and both buttons share one row. The buttons are rendered into their
# column slots further down, AFTER the symbol-check block has computed `ready`
# -- Streamlit columns accept out-of-order writes, which is what lets the
# buttons sit on this row while depending on logic that runs later.
# vertical_alignment="bottom" lines the buttons up with the input boxes
# (labels sit above). The leftover width goes to the two buttons rather than to
# a trailing spacer, so the row ends flush instead of in dead space.
# These weights no longer size the row on their own -- CSS flex rules scoped to
# this row override them. ATR multiple and # Tranches in particular have a width
# floor there that must not be lowered; see the stepper-cliff comment on their
# flex rule for why.
c1, c2, c3, c4, search_col, add_col = st.columns(
    [1.5, 1.0, 1.3, 1.3, 1.9, 1.9], gap="small", vertical_alignment="bottom"
)
symbol = c1.text_input(
    "Symbol, Name", placeholder="HYPE, HYPEUSDT, AAPL, Apple...", key=_symbol_key(),
    on_change=check_symbol,
)
c2.selectbox(
    "Asset Class", options=["All"] + ASSET_CLASSES, index=1, key="add_asset_class_pick",
    on_change=check_symbol,
)
atr_multiple = c3.number_input(
    # Both bounds exist for accessibility, not to constrain trading: see
    # ATR_MULTIPLE_MIN for why the floor has to sit on the step grid, and note
    # that Streamlit emits aria-valuemax="0" on a float number_input with no
    # max, which also announces the field as aria-invalid. 100 is far above any
    # usable multiple, so it only ever blocks a typo. Neither hits # Tranches,
    # which is int-typed and gets a real max from Streamlit.
    "ATR multiple", min_value=ATR_MULTIPLE_MIN, max_value=100.0, value=1.5,
    step=ATR_MULTIPLE_STEP, format="%.1f", key="add_atr_multiple",
    on_change=reset_symbol_check,
)
tranches = c4.number_input(
    "# Tranches", min_value=1, value=3, step=1, key="add_tranches",
    on_change=reset_symbol_check,
)

clean_symbol = symbol.strip().upper()

# Written BEFORE the symbol-check block below even though it sits in a column to
# its left -- Streamlit columns accept out-of-order writes. Order matters here:
# the check block performs the TradingView round-trips, so a button written
# after it can only ever paint its busy state once the work has already
# finished, which is why pressing Search looked like nothing happened. Written
# first, it is on screen and greyed for the whole lookup -- the same two-step
# the sidebar's Refresh button uses.
searching = bool(st.session_state.get("pending_symbol_check"))
_checked = st.session_state.get("symbol_check")
# Deliberately "already FOUND", not "already checked": re-running an identical
# search that found nothing is a legitimate retry (a listing can appear, or the
# earlier attempt can have hit a rate limit), so only a successful result is
# allowed to disable the button.
already_found = bool(
    clean_symbol and _checked and _checked["symbol"] == clean_symbol and _checked["matches"]
)
if search_col.button(
    "🔍 Search", key="search_btn", width="stretch",
    # Complementary to pressing Enter in the Symbol field, not a replacement --
    # useful for re-checking an already-committed symbol (e.g. after ATR
    # multiple/tranches reset the check) without retyping it.
    # This gate is only HALF the answer, and it is the half that lags. Every
    # value here is server-side, and `clean_symbol` is the text_input's
    # COMMITTED value -- Streamlit has no other, since session_state carries
    # the last committed value too and in-flight keystrokes never leave the
    # browser until Enter or blur triggers a rerun. So on its own this greys
    # the button out for exactly the person who has just typed a symbol and not
    # pressed Enter yet, and the click that would have committed it cannot land
    # on a disabled button -- the reason it must stay disabled on an empty
    # field AND go live the moment one is typed is unreachable from here.
    # The browser closes that gap: the gate script below re-derives this same
    # condition against what is actually in the field, on every keystroke,
    # from the verdict published on `symbol_check_results`. What is left here
    # is the state each rerun lands in, which is correct on its own because a
    # rerun is precisely when the committed value and the field agree.
    disabled=searching or already_found or not clean_symbol or not _portfolio_size_set(),
    # Only reasons that do NOT depend on the Symbol field's contents survive
    # here. `help` is React-owned tooltip CONTENT rather than an attribute, so
    # the gate script below cannot keep it honest the way it keeps `disabled`
    # honest -- which makes every field-dependent string wrong at exactly the
    # moment it is read. "Enter a symbol or name first" was appearing over a
    # field with `sol` typed into it, and "Already verified" over a field since
    # edited to a different symbol. Both are gone rather than reworded: no
    # single string is true for both an enabled and a disabled button, and
    # neither state needs one. An empty field sits in plain view next to the
    # greyed-out button, and the verified state prints its own result banner
    # directly below it.
    help=(
        "Checking..." if searching
        else _NO_PORTFOLIO_HINT if not _portfolio_size_set()
        else None
    ),
):
    if clean_symbol:
        check_symbol()
    else:
        # Not routed through check_symbol()'s own empty-symbol branch: that
        # branch is also the Symbol field's on_change, where an empty value
        # means "the user just cleared the field" and a toast would be noise.
        # Here it can only mean a deliberate press with nothing to search.
        queue_toast("Enter a symbol or name first.", "⚠️")
    st.rerun()

# Keep both symbol buttons -- Search and Add Position -- in step with what is
# actually TYPED in the Symbol field, which the server never sees: Streamlit
# commits a text_input only on Enter or blur, so every disabled= around here is
# computed from the last committed value and cannot react to a symbol being
# typed or cleared. Left alone that is a bug in both directions. Search was
# wrong the harmless way -- a freshly typed symbol left it greyed out, and the
# click that would have committed the symbol cannot land on a disabled button.
# Add Position is wrong the dangerous way: it stays enabled over a stale match
# (see the comment on it in sync() below).
# Stateless by design: it recomputes the FULL condition on every keystroke from
# the verdict the server publishes on symbol_check_results, rather than
# remembering whether it was the one that disabled the button. So it can never
# drift out of step with the server, and it can never enable a button the
# server disabled for a reason of its own (no portfolio size, a lookup already
# running) -- those arrive as gate="no" and win here too.
# Same iframe pattern, and for the same reasons, as the sidebar's auto-commit
# helper: st.markdown never executes <script> (it sets innerHTML), and the
# listener is delegated on the parent document because a rerun can remount the
# input and orphan a direct listener. It does NOT commit anything -- the Symbol
# field is deliberately excluded from auto-commit, since committing it fires a
# live TradingView search and a mid-typing pause would search half-typed
# tickers.
components.html(
    """
    <script>
    try {
        const W = window.parent;
        // Hide this helper's own layout slot -- a zero-height iframe still
        // leaves an stElementContainer that is a full flex item and earns a row
        // gap. Matched on contentWindow identity, which cannot go stale the way
        // a testid can, and which touches only this iframe.
        for (const f of W.document.querySelectorAll("iframe")) {
            if (f.contentWindow !== window) continue;
            const slot = f.closest('[data-testid="stElementContainer"]');
            if (slot) slot.style.display = "none";
            break;
        }
        const SYMBOL = "input[aria-label='Symbol, Name']";
        const sync = function () {
            const inp = W.document.querySelector(SYMBOL);
            const btn = W.document.querySelector(".st-key-search_btn button");
            const gate = W.document.querySelector("[data-search-gate]");
            if (!inp || !btn || !gate) return;
            const typed = inp.value.trim().toUpperCase();
            // Mirrors the server's own expression, term for term: its
            // `searching or not _portfolio_size_set()` arrives as the gate, its
            // `already_found` as data-search-found, and its `not clean_symbol`
            // is re-tested here against the live field instead of the committed
            // one. Comparing against the found symbol is what re-opens the
            // button the moment a verified symbol is edited into a new one,
            // rather than only after a commit.
            btn.disabled = gate.dataset.searchGate !== "ok"
                || !typed
                || typed === gate.dataset.searchFound;
            // Add Position needs the same treatment for the opposite reason.
            // The server enables it once a symbol is verified, and binds the
            // match to the click at RENDER time (on_click=..., args=(match,)).
            // Type over a verified symbol without committing and the server
            // sees nothing: the button stays enabled, still carrying the OLD
            // match, so a click adds a position for a symbol the field no
            // longer shows. Requiring the live field to still read as the
            // verified symbol closes that window; the server's own verdict is
            // ANDed in, so this can only ever disable, never enable.
            const add = W.document.querySelector(".st-key-add_position_btn button");
            if (add) {
                add.disabled = gate.dataset.addReady !== "ok"
                    || typed !== gate.dataset.searchFound;
            }
        };
        if (W.__pfSearchGate) {
            W.document.removeEventListener("input", W.__pfSearchGate, true);
        }
        W.__pfSearchGate = function (e) {
            if (e.target && e.target.matches && e.target.matches(SYMBOL)) sync();
        };
        W.document.addEventListener("input", W.__pfSearchGate, true);
        // Keystrokes are not the only thing that can put the button out of
        // step -- a rerun can too, and in a way that is easy to miss.
        // Streamlit renders through React, which writes an attribute only when
        // its OWN previous value for it changed. Once this script has written
        // `disabled` straight to the DOM, React's copy no longer matches the
        // DOM, and any later render that arrives at the same value React
        // already believed is skipped -- leaving this script's stale value in
        // place. Measured: after a search committed from a typed symbol, the
        // server correctly re-rendered the button as disabled ("already
        // verified") and the DOM stayed enabled.
        // Re-deriving on mutations fixes it from either direction, and cannot
        // loop: sync() only ever assigns, and assigning a value a property
        // already holds mutates nothing, so the observer goes quiet by itself.
        // Filtered to just the attributes below -- the only ones that can
        // change the answer -- so a rerun's ordinary DOM churn does not wake
        // it (the grid is in its own iframe and never reaches this document).
        if (W.__pfSearchGateObs) W.__pfSearchGateObs.disconnect();
        W.__pfSearchGateObs = new W.MutationObserver(function () {
            clearTimeout(W.__pfSearchGateTimer);
            W.__pfSearchGateTimer = setTimeout(sync, 0);
        });
        W.__pfSearchGateObs.observe(W.document.body, {
            subtree: true, childList: true, attributes: true,
            attributeFilter: [
                "disabled", "data-search-gate", "data-search-found", "data-add-ready",
            ],
        });
        // Once on install: normally a no-op, since a rerun is the one moment
        // the field and the committed value agree. It matters when Streamlit
        // restores a typed-but-uncommitted value into a remounted input, where
        // no input event ever fires.
        sync();
    } catch (e) {
        // This iframe is invisible (height 0), so without this an install
        // failure -- a renamed label, blocked parent access -- would take the
        // feature down with no signal anywhere, not even the console.
        console.error("[search-gate] install failed", e);
    }
    </script>
    """,
    height=0,
)


# Everything the symbol check renders (spinner, warnings, match panel) lands
# inside this container.
with st.container(key="symbol_check_results"):
    # Streamlit deletes an EMPTY container from the DOM outright, and a
    # min-height reserves nothing on an element that isn't there. The
    # zero-height div that fills this slot keeps the container mounted, so the
    # reservation above holds in the empty state too -- which is the state it
    # exists for. It doubles as the gate script's channel to the server.
    #
    # Filled after the branch chain below rather than here (see
    # _gate_slot.html), because one of the attributes it publishes is Add
    # Position's `ready`, which the chain has not worked out yet at this point
    # in the run. The placeholder keeps the element in THIS container at THIS
    # position while its contents are decided later in the same run.
    _gate_slot = st.empty()

    # pop, not get: the confirmation is one-shot. add_position() sets it and
    # Streamlit reruns once to paint it; consuming it here means the NEXT rerun
    # -- whatever the user does, delete the row, search again, hit refresh --
    # renders without it. Reading it non-destructively left a green "PEPEUSDT
    # added to Positions" standing over an empty table after the row was
    # deleted. add_position() also clears pending_symbol_check, so the spinner
    # branch below cannot rerun on the same pass that popped this message and
    # throw the st.success away before it paints.
    added_message = st.session_state.pop("add_success", None)
    if added_message:
        st.success(added_message)

    pending = st.session_state.get("pending_symbol_check")
    if pending:
        with st.spinner(f"Checking {_md_escape(pending['symbol'])} on TradingView..."):
            run_pending_symbol_check(pending["symbol"], pending["picked_class"])
        st.session_state.pending_symbol_check = None
        # The Search button rendered itself disabled ("Checking...") earlier in
        # THIS run, before the lookup started, and Streamlit will not repaint it
        # now that the answer is in. Rerun so it settles into the right state --
        # without this a search that found nothing would leave the button stuck
        # disabled with no way to try again. No round-trips on the way back
        # through: the result is already in session_state.
        st.rerun()

    check = st.session_state.get("symbol_check")
    match = bool(check and clean_symbol and check["symbol"] == clean_symbol)

    chosen_match = None
    # Why Add Position is disabled, for its tooltip. A disabled button with no
    # explanation is a dead end, and the reasons are spread across the
    # branches below -- this is the only thing that carries them to the button,
    # which is rendered after this whole block. Stays None on the success path,
    # which is what leaves an enabled button with no tooltip.
    add_help = None
    if not _portfolio_size_set():
        # First in the chain for the same reason the Search button ranks it
        # first: without a portfolio size the rest of the flow cannot produce a
        # number, so "verify the symbol first" would be a true statement that
        # leads nowhere.
        ready = False
        add_help = _NO_PORTFOLIO_HINT
    elif not match:
        # This covers the empty field too: `match` is False whenever
        # clean_symbol is "", so the separate "Enter a symbol or name first"
        # branch that used to sit above this one was both redundant and untrue.
        # clean_symbol is the COMMITTED value, so it reads empty while the user
        # is still typing, and that tooltip claimed nothing had been entered
        # while a symbol was sitting in the field. Naming no symbol is the point
        # of this wording -- it holds whether the field is empty or holds text
        # that has not been committed yet, and the action it asks for is the
        # same either way.
        ready = False
        add_help = "Press Enter in the Symbol field (or click Search) to verify a symbol first"
    elif not check["matches"]:
        upstream_issues = [e for e in check.get("errors", {}).values() if e and e.get("retryable")]
        if upstream_issues:
            # A genuine upstream refusal outranks our own deadline when both are
            # present: it is the one the user can actually act on (wait a
            # moment), whereas a timeout just means "try again now". Blaming
            # rate-limiting for our own expired budget sent people off waiting
            # for a limit that was never applied.
            issue = next((e for e in upstream_issues if not e.get("timeout")), upstream_issues[0])
            if issue.get("timeout"):
                cause = "the lookup ran out of time before TradingView answered"
            else:
                wait_hint = f" (TradingView suggests waiting ~{issue['retry_after_s']:.0f}s)" if issue.get("retry_after_s") else ""
                cause = f"TradingView is rate-limiting or erroring on requests{wait_hint}"
            st.warning(
                f"⚠️ Couldn't verify **{_md_escape(clean_symbol)}** right now — {cause}. "
                f"This does NOT mean the symbol doesn't exist."
            )
            add_help = "Couldn't reach TradingView to verify this symbol — use Retry check above"
            if st.button("🔄 Retry check"):
                st.session_state.pending_symbol_check = {
                    "symbol": clean_symbol,
                    "picked_class": st.session_state.get("add_asset_class_pick", "All"),
                }
                st.rerun()
        else:
            classes_checked = check.get("classes_checked", ASSET_CLASSES)
            if len(classes_checked) == 1:
                classes_text = classes_checked[0]
            elif len(classes_checked) == 2:
                classes_text = " or ".join(classes_checked)
            else:
                classes_text = ", ".join(classes_checked[:-1]) + f", or {classes_checked[-1]}"
            st.warning(f"**{_md_escape(clean_symbol)}** wasn't found as a {classes_text} symbol on any exchange we checked.")
            add_help = f"{_md_escape(clean_symbol)} wasn't found as a {classes_text} symbol — check the spelling or the asset class"
        ready = False
    elif len(check["matches"]) > 1:
        matches = check["matches"]
        # Options are INDICES, not labels: two listings can share a ticker
        # across venues, and a duplicate label would make st.radio ambiguous.
        # The key is scoped to the searched symbol so a stale index cannot
        # carry over -- index 1 meaning TCPC in one search and Solayer in the
        # next is how a picker silently picks the wrong company.
        picked = st.radio(
            f"**{_md_escape(clean_symbol)}** matches more than one — which one did you mean?",
            range(len(matches)),
            format_func=lambda i: match_choice_label(matches[i]),
            key=f"add_match_choice_{check['symbol']}",
        )
        chosen_match = matches[picked]
    else:
        chosen_match = check["matches"][0]

    if chosen_match is not None:
        price = chosen_match["atr_info"]["current_price"]
        if price is None:
            st.warning(f"**{_md_escape(clean_symbol)}** verified but TradingView returned no live price; can't auto-fill entry price.")
            ready = False
            add_help = "TradingView returned no live price for this symbol, so the position can't be sized"
        else:
            # A duplicate is the same symbol/asset class/exchange with the exact same
            # sizing settings -- those settings are what the position's numbers are
            # computed from, so any one of them differing is a legitimately different
            # position (e.g. same symbol re-added with a different ATR multiple).
            # ATR multiple compares at its 1-decimal display precision ("%.1f"):
            # stepper arithmetic and typed input can differ in invisible low bits,
            # which would let two visually identical positions pass this check.
            duplicate = next(
                (
                    p for p in st.session_state.positions
                    if p["symbol"] == chosen_match["symbol"]
                    and p["asset_class"] == chosen_match["asset_class"]
                    and p.get("exchange") == chosen_match["exchange"]
                    and round(p["atr_multiple"], 1) == round(atr_multiple, 1)
                    and p["tranches"] == int(tranches)
                ),
                None,
            )
            if duplicate is not None:
                st.warning(
                    f"You already have **{_md_escape(chosen_match['symbol'])}** "
                    f"({_md_escape(chosen_match['exchange'])}) "
                    f"with these same settings in your table."
                )
                ready = False
                add_help = ("Already in your table with these same settings — change the ATR multiple "
                            "or tranches to add it as a separate position")
            else:
                # display_name is TradingView's own "description" field -- the
                # least trustworthy string in this message, and the only one a
                # third party writes.
                name_suffix = (
                    f" ({_md_escape(chosen_match['display_name'])})"
                    if chosen_match.get("display_name") else ""
                )
                # One line carries the whole result: it replaced a grey
                # "matched X by name -> TICKER" caption plus three st.metric
                # cards (Asset Class / Exchange / Live Price) that stood above it.
                # The caption was duplication by construction, not just visually
                # -- the only branch that set it ran when there was exactly one
                # match, and the ticker it printed was that match's own symbol,
                # the same string this line already shows. Its "by name" wording
                # was wrong for plain ticker expansion too, since it fired
                # whenever the resolved symbol differed from the typed text, and
                # BTC -> BTCUSD is not a name match.
                # The cards spent ~90px of height on three short values; all
                # three are inline here, so only the space was lost.
                st.success(
                    f"✅ {_md_escape(chosen_match['symbol'])}{name_suffix} found on "
                    f"{_md_escape(chosen_match['exchange'])} — {chosen_match['asset_class']}, "
                    f"${price:,.2f}, ATR ({st.session_state.timeframe_label}) "
                    f"{chosen_match['atr_info']['atr_value']:.2f}"
                )
                ready = True

# Published now that the whole chain has run, into the slot reserved inside
# symbol_check_results above. These attributes carry everything the gate script
# cannot work out for itself: the conditions that have nothing to do with the
# field's contents, the symbol already verified, and Add Position's own verdict.
# Hung on a zero-height div deliberately -- it is already mounted for the
# container's min-height, so publishing costs no new element and cannot alter
# the layout, which a fresh st.html next to the buttons would (every element is
# a flex item and earns a row gap).
_gate_slot.html(
    "<div style='height:0'"
    f" data-search-gate='{'ok' if not searching and _portfolio_size_set() else 'no'}'"
    # html.escape, not _md_escape: this lands in an HTML attribute, not
    # markdown, and the value is whatever the user typed into the Symbol
    # field. quote=True is what closes the attribute itself.
    f" data-search-found='{html.escape(_checked['symbol'], quote=True) if already_found else ''}'"
    # Add Position's server-side verdict, which depends on things no client-side
    # script can re-derive: whether the symbol resolved, which of several
    # matches is picked, and whether it is already in the table. The script ANDs
    # this with its own live-field test, so it can only ever add restriction.
    f" data-add-ready='{'ok' if ready else 'no'}'"
    "></div>"
)

# The append/save/clear work lives in the add_position on_click callback:
# callbacks run before the next rerun renders any widget, which is what lets
# the callback rotate the Symbol field's key (see _symbol_key) so the field
# comes back empty. `ready` is True only when chosen_match is not None, and a
# disabled button never fires its callback, so the args are always a real match.
add_col.button(
    "➕ Add Position", key="add_position_btn", disabled=not ready, width="stretch",
    help=add_help,
    on_click=add_position, args=(chosen_match,),
)

# ── Positions table ───────────────────────────────────────────────────────────
st.subheader("Positions")

# Filtered by id (not just present) so a since-removed position's diff doesn't
# linger in this table after refresh_all_positions stored it.
positions_by_id = {pos["id"]: pos for pos in st.session_state.positions}
current_ids = set(positions_by_id)
change_rows = []
for c in st.session_state.get("last_refresh_changes") or []:
    if c["id"] not in current_ids:
        continue
    if c["old_entry_price"] == c["new_entry_price"] and c["old_atr_value"] == c["new_atr_value"]:
        continue
    change_rows.append({
        "Symbol": format_symbol_display(c["symbol"], positions_by_id[c["id"]]["asset_class"]),
        "Old Entry Price ($)": f"{c['old_entry_price']:,.2f}",
        "New Entry Price ($)": f"{c['new_entry_price']:,.2f}",
        "Old ATR": f"{c['old_atr_value']:,.2f}",
        "New ATR": f"{c['new_atr_value']:,.2f}",
    })
if SHOW_LAST_REFRESH_CHANGES and change_rows:
    st.markdown("🔄 **Changed on last refresh:**")
    changes_df = pd.DataFrame(change_rows)
    gb_changes = GridOptionsBuilder.from_dataframe(changes_df)
    gb_changes.configure_default_column(
        resizable=True, sortable=False, filter=False, suppressHeaderMenuButton=True
    )
    gb_changes.configure_grid_options(rowHeight=GRID_ROW_HEIGHT, headerHeight=GRID_HEADER_HEIGHT)
    AgGrid(
        changes_df,
        gridOptions=gb_changes.build(),
        height=GRID_HEADER_HEIGHT + GRID_ROW_HEIGHT * len(change_rows),
        update_mode=GridUpdateMode.NO_UPDATE,
        theme="alpine",
        key="changes_grid",
    )

if not st.session_state.positions:
    st.info("No positions yet — add one above.")
else:
    rows = []
    warnings = []
    for pos in st.session_state.positions:
        # No live TradingView call here — every field below is whatever was last
        # fetched by "Add position" or "Refresh price & ATR". This is what makes
        # reloading the page (or any other rerun) instant and network-free.
        row = {
            "id": pos["id"],
            "Symbol": format_symbol_display(pos["symbol"], pos["asset_class"]),
            "Name": pos.get("display_name") or "",
            "Asset Class": pos["asset_class"],
            "Exchange": pos.get("exchange"),
            "Date Added/Refreshed": pos.get("added_at"),
            "Entry Price ($)": pos["entry_price"],
            "ATR Multiple": pos["atr_multiple"],
            "# Tranches": pos["tranches"],
        }

        atr_value = pos.get("atr_value")
        if pos.get("fetch_error"):
            warnings.append(
                f"**{_md_escape(pos['symbol'])}**: last refresh failed "
                f"({_md_escape(pos['fetch_error'])})"
                + (" — showing last known ATR." if atr_value is not None else "")
            )
        if pos.get("resolution_note"):
            # The ℹ️ prefix stays outside the escape -- the warnings loop below
            # dispatches st.info vs st.warning on it.
            warnings.append(f"ℹ️ {_md_escape(pos['resolution_note'])}")

        if atr_value is None:
            row.update({
                "ATR": None, "Stop Price": None, "Stop Dist (%)": None,
                "Position Size ($)": None, "Position Size (%)": None,
                "Tranche Size ($)": None, "Tranche Size (%)": None,
            })
        else:
            outputs = compute_outputs(
                pos["entry_price"], atr_value, pos["atr_multiple"],
                pos["tranches"], st.session_state.portfolio_size, st.session_state.risk_pct,
            )
            row["ATR"] = atr_value
            if outputs is None:
                warnings.append(f"**{_md_escape(pos['symbol'])}**: can't compute position size (check entry price / ATR / portfolio size).")
                row.update({
                    "Stop Price": None, "Stop Dist (%)": None,
                    "Position Size ($)": None, "Position Size (%)": None,
                    "Tranche Size ($)": None, "Tranche Size (%)": None,
                })
            elif not outputs["stop_reachable"]:
                # Stop Dist (%) stays on screen -- reading ">= 100%" is what
                # makes the warning make sense -- but the stop price and every
                # size are blanked rather than shown: a negative stop can't be
                # placed, and the sizes derived from it are arbitrary (they
                # come out *below* the risk budget, which is impossible for a
                # valid setup). No dollar amounts in the message: st.warning
                # renders markdown, where a second "$" pairs with the first
                # into a LaTeX span that eats both.
                ceiling = max_safe_atr_multiple(pos["entry_price"], atr_value)
                fix = (
                    f"Lower it to {ceiling:.1f} or less"
                    if ceiling is not None
                    else "The ATR is larger than the entry price itself, so check the data"
                )
                warnings.append(
                    f"**{_md_escape(pos['symbol'])}**: ATR multiple {pos['atr_multiple']:.1f} is wider than "
                    f"the whole entry price on the {st.session_state.timeframe_label} "
                    f"timeframe, so the stop lands at or below zero and can't be placed. "
                    f"{fix}, or use a shorter ATR timeframe."
                )
                row.update({
                    "Stop Price": None,
                    "Stop Dist (%)": outputs["stop_distance_pct"] * 100,
                    "Position Size ($)": None, "Position Size (%)": None,
                    "Tranche Size ($)": None, "Tranche Size (%)": None,
                })
            else:
                row["Stop Price"] = outputs["stop_price"]
                row["Stop Dist (%)"] = outputs["stop_distance_pct"] * 100
                row["Position Size ($)"] = outputs["position_size_dollars"]
                row["Position Size (%)"] = outputs["position_size_pct"] * 100
                row["Tranche Size ($)"] = outputs["tranche_size_dollars"]
                row["Tranche Size (%)"] = outputs["tranche_size_pct"] * 100

        rows.append(row)

    for w in warnings:
        if w.startswith("ℹ️"):
            st.info(w)
        else:
            st.warning(w)

    df = pd.DataFrame(rows)
    PREFERRED_ORDER = [
        "Symbol", "Asset Class",
        "Stop Price", "Position Size ($)", "Tranche Size ($)",
        "Entry Price ($)", "ATR Multiple", "# Tranches",
        "ATR", "Stop Dist (%)", "Position Size (%)", "Tranche Size (%)",
        "Date Added/Refreshed", "Exchange",
    ]
    display_cols = [c for c in PREFERRED_ORDER if c in df.columns]

    # "Name" (company/coin name) is hidden -- it exists only so the grid's quick
    # filter can match a full name search (e.g. "Apple") against a row whose
    # visible Symbol column only shows "AAPL". includeHiddenColumnsInQuickFilter
    # (set below) makes the one search box match both at once.
    grid_cols = display_cols + ["Name", "id"]

    def grid_formatter(digits: int) -> JsCode:
        return JsCode(
            "function(params){ return (params.value === null || params.value === undefined) "
            "? '' : Number(params.value).toLocaleString('en-US', "
            f"{{minimumFractionDigits: {digits}, maximumFractionDigits: {digits}}}); }}"
        )
    HIGHLIGHT_STYLES = {
        "Stop Price": "function(params){ return {backgroundColor: '#ffe1e1', fontWeight: 'bold'}; }",
        "Position Size ($)": "function(params){ return {backgroundColor: '#e1f7e1', fontWeight: 'bold'}; }",
        "Tranche Size ($)": "function(params){ return {backgroundColor: '#e1edff', fontWeight: 'bold'}; }",
    }

    gb = GridOptionsBuilder.from_dataframe(df[grid_cols])
    gb.configure_default_column(
        resizable=True, sortable=True, filter=False, suppressHeaderMenuButton=True,
        wrapHeaderText=False,
    )
    gb.configure_column("id", hide=True)
    gb.configure_column("Name", hide=True)
    # header_name changes display text only -- the colId/field stays the plain
    # column name, so the edited_row[...] push-back below and _FIT_COLUMNS_JS
    # are unaffected. The ✏️ marks the editable columns (nothing else
    # visually distinguishes them from read-only cells -- UX review).
    # Entry Price stays read-only on purpose: it comes from the live price
    # feed and "🔄 Refresh Price & ATR" is the way to update it.
    gb.configure_column("Entry Price ($)",
                        type=["numericColumn"], valueFormatter=grid_formatter(2))
    gb.configure_column("ATR Multiple", header_name="✏️ ATR Multiple",
                        type=["numericColumn"], valueFormatter=grid_formatter(1), editable=True)
    gb.configure_column("# Tranches", header_name="✏️ # Tranches",
                        type=["numericColumn"], editable=True)
    COLUMN_DIGITS = {
        "ATR": 2, "Stop Price": 2, "Stop Dist (%)": 0,
        "Position Size ($)": 0, "Position Size (%)": 2,
        "Tranche Size ($)": 0, "Tranche Size (%)": 2,
    }
    for col, digits in COLUMN_DIGITS.items():
        gb.configure_column(
            col, type=["numericColumn"], valueFormatter=grid_formatter(digits),
            cellStyle=JsCode(HIGHLIGHT_STYLES[col]) if col in HIGHLIGHT_STYLES else None,
        )
    gb.configure_selection(
        selection_mode="multiple", use_checkbox=True,
        header_checkbox=True, header_checkbox_filtered_only=False,
    )
    # columns_auto_size_mode on AgGrid() is a no-op in this installed st_aggrid
    # version (unused in AgGrid.py) — autoSizeStrategy is the real mechanism
    # that sizes each column to its own header/content instead of stretching
    # every column to a uniform width.
    # suppressColumnVirtualisation is required for accurate autosizing here:
    # with many columns some sit off-screen at initial render, and a virtualized
    # grid skips measuring cells it hasn't mounted, so those columns fall back
    # to ag-grid's 200px default width instead of their real content width.
    # fitCellContents sizes each column to its own content, which usually leaves
    # the sum of column widths short of the grid's actual viewport width.
    # Calling plain sizeColumnsToFit() to close that gap stretches EVERY column
    # proportionally, which pads out short-content columns (e.g. Symbol) with
    # wasted trailing space. Instead, lock every column at its current width
    # via columnLimits (minWidth == maxWidth) except one designated filler
    # column, which absorbs leftover space -- but only when there IS leftover
    # space. If content already fills or exceeds the viewport, the filler is
    # locked to its own natural fitCellContents width too (never shrunk below
    # it), so no column or header is ever squeezed/wrapped; the grid scrolls
    # horizontally instead. The filler's natural width is re-measured on every
    # pass via autoSizeColumns: caching the first-seen width goes stale when a
    # later row brings a longer Exchange value (its cap would then squeeze the
    # content), while reading the live width without re-shrinking first would
    # ratchet upward after any stretch.
    # Bound to both onFirstDataRendered (initial fitCellContents result) and
    # onGridSizeChanged (fires on browser window resize) so the layout stays
    # correct as the window is resized, not just on first paint.
    # getColumnState() includes HIDDEN columns (Name, id) with real widths --
    # summing those into lockedTotal claims ~2 phantom columns of content, which
    # flips the "does content overflow?" check to yes, locks the filler, and
    # leaves an empty strip right of the last column. Only visible columns
    # count. A 0-width viewport (not laid out yet at the 50ms mark) must retry,
    # not proceed -- proceeding takes the lock-everything branch by accident.

    # When the window is too narrow for every column, the table just scrolls --
    # and on a trackpad with overlay scroll bars there is nothing on screen
    # saying so, which is how a reader concludes the columns to the right do not
    # exist. This puts a round chevron on each border of the table, centred
    # vertically, each one showing whenever there are columns hidden that way.
    # Text was tried there first and read as part of whatever it sat next to
    # ("Tranche Size ($)" plus a "more" pill scans as one label); an icon on the
    # edge it points at does not.
    #
    # Every handler below re-evaluates the same condition, so the body is
    # installed on window once and they all call it: each JsCode is eval'd as
    # an independent function and cannot otherwise share a definition.
    #
    # The argument splits the cheap half from the expensive one. Only a resize
    # or a re-render can move a row, and the vertical placement below measures
    # every row to find one -- so a horizontal scroll, which fires many times a
    # second and cannot change any row's top, asks for the visibility half
    # alone. Measuring on every tick forced a reflow per row per event.
    _MORE_HINT_INSTALL_JS = (
        "if (!window.pscSyncMoreHint) { window.pscSyncMoreHint = function(remeasure){ "
        "var vp = document.querySelector('.ag-center-cols-viewport'); "
        "if (!vp) { return; } "
        "var wrapper = vp.closest('.ag-root-wrapper'); "
        "if (!wrapper) { return; } "
        # One builder for both, so the two buttons cannot drift apart in size,
        # colour or scroll step -- only the direction differs.
        "var build = function(cls, points, dir, label){ "
        "var el = wrapper.querySelector('.' + cls); "
        "if (el) { return el; } "
        "el = document.createElement('div'); "
        "el.className = 'psc-scroll-hint ' + cls; "
        "el.title = label; "
        "el.setAttribute('role', 'button'); "
        "el.setAttribute('aria-label', label); "
        # Announced as a button, so it has to work like one: a bare div with
        # role=button is not in the tab order and Enter/Space do nothing on it.
        # tabIndex is set here for the case where the element is built already
        # visible, and re-set by show() below on every state change.
        "el.tabIndex = -1; "
        # An SVG rather than a "›" glyph: the chevron characters render at
        # wildly different sizes and baselines across fonts, so a text one
        # cannot be reliably centred in a 20px circle.
        "el.innerHTML = '<svg viewBox=\"0 0 24 24\" width=\"12\" height=\"12\" "
        "fill=\"none\" stroke=\"#5a6270\" stroke-width=\"3.5\" "
        "stroke-linecap=\"round\" stroke-linejoin=\"round\">"
        "<polyline points=\"' + points + '\"></polyline></svg>'; "
        # A round chevron reads as a control, so it behaves like one. The
        # viewport is re-queried on each click rather than captured: ag-Grid
        # rebuilds these nodes on a column change, and a stale reference would
        # scroll a detached element. It mirrors the header and pinned viewports
        # off this one's scroll event, so setting scrollLeft is the whole job.
        "var scroll = function(){ "
        "var v = document.querySelector('.ag-center-cols-viewport'); "
        "if (v) { v.scrollLeft += dir * v.clientWidth * 0.8; } }; "
        "el.addEventListener('click', scroll); "
        # Space would scroll the page as well without the preventDefault, so
        # one keypress would move both the table and the document.
        "el.addEventListener('keydown', function(e){ "
        "if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { "
        "e.preventDefault(); scroll(); } }); "
        "wrapper.appendChild(el); "
        "return el; "
        "}; "
        "var next = build('psc-more-cols', '9 5 16 12 9 19', 1, "
        "'Scroll right for more columns'); "
        "var prev = build('psc-prev-cols', '15 5 8 12 15 19', -1, "
        "'Scroll left for earlier columns'); "
        # Mid-table vertically, but snapped to the nearest gap BETWEEN rows
        # rather than the literal 50%. The circles are 20px on a 42px row, so
        # sitting in a row's middle puts them over that row's figures -- with
        # the numbers left-aligned, the left-hand one was measured covering 22
        # of the 30px of a "7.88". A row boundary is the one horizontal strip
        # with no text in it, because a 42px row centres roughly 14px of
        # glyphs. The move is a few pixels and reads as the middle either way.
        # Measured, not derived from GRID_ROW_HEIGHT: a wrapped header or a
        # theme that rounds row heights would put a computed offset back inside
        # the text. The header/first-row boundary counts as a candidate so that
        # a one-position table still gets a real gap -- it overflows just the
        # same, and 50% of a header plus one row lands in that row's digits.
        # With more rows the nearest-to-middle test never picks it anyway. The
        # boundary below the LAST row is excluded: the circle would hang half
        # outside the grid, where it gets clipped.
        "var rows = remeasure ? wrapper.querySelectorAll("
        "'.ag-center-cols-container .ag-row') : []; "
        "if (rows.length > 0) { "
        "var wb = wrapper.getBoundingClientRect(); "
        "var mid = wb.height / 2, best = null; "
        "for (var i = 0; i < rows.length; i++) { "
        "var edge = rows[i].getBoundingClientRect().top - wb.top; "
        "if (best === null || Math.abs(edge - mid) < Math.abs(best - mid)) { "
        "best = edge; } "
        "} "
        "next.style.top = best + 'px'; "
        "prev.style.top = best + 'px'; "
        "} "
        # Fractional column widths leave scrollWidth a fraction above
        # clientWidth on a table that fits perfectly well, so a bare "> 0"
        # would leave the right-hand chevron showing permanently on a maximised
        # window. The left one uses the same tolerance for symmetry.
        # Hidden has to mean gone from the accessibility tree and the tab order
        # too, not merely transparent: both circles stay in the DOM at every
        # window width, so a screen reader or a Tab key would otherwise reach
        # two buttons that scroll nothing.
        "var show = function(el, on){ "
        "el.classList.toggle('psc-visible', on); "
        "el.setAttribute('aria-hidden', on ? 'false' : 'true'); "
        "el.tabIndex = on ? 0 : -1; "
        "}; "
        "var hidden = vp.scrollWidth - vp.clientWidth - vp.scrollLeft; "
        "show(next, hidden > 2); "
        "show(prev, vp.scrollLeft > 2); "
        "}; } "
    )
    _MORE_HINT_JS = JsCode(
        "function(params){ " + _MORE_HINT_INSTALL_JS + "window.pscSyncMoreHint(); }"
    )
    _FIT_COLUMNS_JS = JsCode(
        "function(params){ "
        + _MORE_HINT_INSTALL_JS +
        "var attempt = function(tries){ "
        "var FILLER = 'Exchange'; "
        "var visible = params.api.getColumnState().filter(function(s){ return !s.hide; }); "
        "var fillerState = visible.find(function(s){ return s.colId === FILLER; }); "
        "if (!fillerState) { return; } "
        "var vp = document.querySelector('.ag-center-cols-viewport'); "
        "var viewportWidth = vp ? vp.clientWidth : 0; "
        "if (viewportWidth === 0) { "
        "if (tries > 0) { setTimeout(function(){ attempt(tries - 1); }, 100); } "
        "return; "
        "} "
        "params.api.autoSizeColumns([FILLER]); "
        "var fillerNatural = params.api.getColumnState()"
        ".find(function(s){ return s.colId === FILLER; }).width; "
        "var lockedTotal = 0; "
        "visible.forEach(function(s){ if (s.colId !== FILLER) lockedTotal += s.width; }); "
        "var limits = visible "
        ".filter(function(s){ return s.colId !== FILLER; }) "
        ".map(function(s){ return {key: s.colId, minWidth: s.width, maxWidth: s.width}; }); "
        "if (lockedTotal + fillerNatural < viewportWidth) { "
        "limits.push({key: FILLER, minWidth: fillerNatural}); "
        "} else { "
        "limits.push({key: FILLER, minWidth: fillerNatural, maxWidth: fillerNatural}); "
        "} "
        "params.api.sizeColumnsToFit({columnLimits: limits}); "
        # Deferred a tick: the widths above are applied to the DOM before the
        # browser recomputes scrollWidth, so reading it here still returns the
        # pre-resize value and the hint would lag one resize behind.
        "setTimeout(function(){ window.pscSyncMoreHint(true); }, 0); "
        "}; "
        "setTimeout(function(){ attempt(5); }, 50); }"
    )
    # Same auto-apply idea as the sidebar/add-form inputs (see the
    # components.html iframe in the sidebar), but with a longer 4s pause:
    # unlike a whole form field, a cell editor committing mid-thought saves a
    # HALF-TYPED number ("8" while typing "82") -- verified live, the 1.5s
    # timer did exactly that. Commit goes via ag-grid's own stopEditing(),
    # the same path Enter/click-away take, so the MODEL_CHANGED push-back
    # below runs unchanged. The parent page's delegated listener can't be
    # reused here because the grid renders in its own iframe. Before
    # stopping, re-check that the cell being edited is still the one this
    # timer was armed for -- the user may have tabbed to another cell just
    # before the timer fired.
    _AUTO_COMMIT_EDIT_JS = JsCode(
        "function(params){ "
        "setTimeout(function(){ "
        "var el = document.activeElement; "
        "if (!el || el.tagName !== 'INPUT') { return; } "
        "var timer = null; "
        "el.addEventListener('input', function(){ "
        "clearTimeout(timer); "
        "timer = setTimeout(function(){ "
        "if (!el.value || !el.value.trim()) { return; } "  # empty = mid-retype; only explicit Enter/click-away may commit it
        "var ec = params.api.getEditingCells(); "
        "if (ec.length && ec[0].rowIndex === params.rowIndex "
        "&& ec[0].column.getColId() === params.column.getColId()) { "
        "params.api.stopEditing(); "
        "} "
        "}, 4000); "
        "}); "
        "}, 0); }"
    )
    gb.configure_grid_options(
        onCellEditingStarted=_AUTO_COMMIT_EDIT_JS,
        # Deliberately NO getRowId here: st_aggrid injects a hidden
        # ::auto_unique_id:: column and wires its own getRowId to it, which
        # both enables ag-grid delta row updates (keepRenderedRows) and is
        # what AgGridReturn uses to reindex returned nodes. Supplying a
        # custom getRowId suppresses that column and every returned value
        # comes back NaN after the reindex.
        autoSizeStrategy={"type": "fitCellContents"},
        # fitCellContents adds autoSizePadding (default 20px) of slack per
        # column; with right-aligned numeric columns it all lands left of the
        # header label, reading as lopsided header padding. 0 is too tight —
        # the measurement misses the 6px+6px custom_css cell padding + 1px
        # border, wrapping headers and truncating cells — so cover exactly that.
        autoSizePadding=13,
        suppressColumnVirtualisation=True,
        rowHeight=GRID_ROW_HEIGHT,
        headerHeight=GRID_HEADER_HEIGHT,
        includeHiddenColumnsInQuickFilter=True,
        onFirstDataRendered=_FIT_COLUMNS_JS,
        onGridSizeChanged=_FIT_COLUMNS_JS,
        # Scrolling is the one thing that changes how much is still hidden
        # without changing any column width, so it needs its own handler.
        onBodyScroll=_MORE_HINT_JS,
    )
    grid_options = gb.build()

    # AgGrid renders inside an iframe, so the host page's CSS can't reach grid
    # internals — column dividers/padding have to go through custom_css instead.
    GRID_CSS = {
        # Balham's 12px default reads too small at this row height; autosizing
        # measures the rendered font, so column widths track this size change.
        # Deliberately 14px rather than the 16px every other text surface in
        # the app uses: the table is 14 columns wide, and at 16px autosizing
        # took its content to 1887px inside a 1578px viewport -- a horizontal
        # scrollbar on the app's primary output, where 14px fits with room to
        # spare. This is the one place a smaller size buys something.
        ".ag-cell": {
            "border-right": "1px solid #d5d5d5 !important",
            "padding-left": "6px !important",
            "padding-right": "6px !important",
            "font-size": "14px !important",
        },
        # type=["numericColumn"] is kept for its numeric sort and filter, but the
        # right alignment that comes with it puts every digit against the far
        # edge of its column -- which is the part that gets clipped when the
        # table is wider than the window, so a half-visible column showed an
        # empty cell rather than a number. Left-aligned, a value is readable as
        # soon as any part of its column is on screen.
        ".ag-cell.ag-right-aligned-cell": {
            "text-align": "left !important",
            "justify-content": "flex-start !important",
        },
        # The same 14px as the cells below them. These were 13px -- the
        # smallest text anywhere in the app, and smaller than the values they
        # label, which reads as a mistake rather than a hierarchy. The header
        # row already sets itself apart by weight and background.
        ".ag-header-cell": {
            "border-right": "1px solid #c2c2c2 !important",
            "padding-left": "6px !important",
            "padding-right": "6px !important",
            "font-size": "14px !important",
        },
        # Centering makes header gaps symmetric BY CONSTRUCTION: autoSizePadding
        # slack otherwise piles entirely onto the empty side of an aligned label
        # (left of right-aligned numeric headers), reading as lopsided padding.
        ".ag-header-cell-label": {
            "padding": "0 !important",
            "justify-content": "center",
        },
        '.ag-header-cell[col-id="Stop Price"] .ag-header-cell-text': {
            "font-weight": "700",
        },
        '.ag-header-cell[col-id="Position Size ($)"] .ag-header-cell-text': {
            "font-weight": "700",
        },
        '.ag-header-cell[col-id="Tranche Size ($)"] .ag-header-cell-text': {
            "font-weight": "700",
        },
        # Under classic scrollbars (Linux, Windows) ag-Grid gives the
        # horizontal scroll bar a real 15px flex slot below the body viewport,
        # so the viewport shrinks by 15px inside an iframe whose height is
        # already fixed at grid_height and the last row is sliced in half.
        # Measured on Debian 12 / Chromium 152 at a 500px width: viewport 108
        # against 126px of rows, 18 of the last row's 42 pixels cut off.  macOS
        # overlay scrollbars do not hit this -- ag-Grid detects them and sets
        # this widget position:absolute itself. Doing the same unconditionally
        # takes the bar out of flow everywhere, which restored the viewport to
        # 123 (the macOS number exactly) and costs macOS nothing.
        ".ag-body-horizontal-scroll": {
            "position": "absolute !important",
            "bottom": "0",
            "left": "0",
            "right": "0",
        },
        # Built and toggled by _MORE_HINT_INSTALL_JS above. Absolutely
        # positioned inside .ag-root-wrapper (already position:relative) so the
        # pair costs no layout height -- grid_height below stays exactly a
        # header plus its rows, which is what keeps the last row from clipping.
        # Each is centred on the border it points at: a white disc with a grey
        # rim. The chevron's #5a6270 on white measures about 6:1, well past the
        # 3:1 WCAG 1.4.11 asks of a graphical control.
        ".psc-scroll-hint": {
            "position": "absolute",
            "top": "50%",
            "transform": "translateY(-50%)",
            "display": "flex",
            "align-items": "center",
            "justify-content": "center",
            "width": "20px",
            "height": "20px",
            "border-radius": "50%",
            "background": "#ffffff",
            # The rim is load-bearing, not decoration: an all-white disc on
            # white cells has no edge at all, and the grey also stops the cell
            # and header borders underneath from appearing to run through it.
            # #c2c2c2 is the same grey as the grid's own cell borders.
            # A box-shadow ring rather than a border, so the rim does not
            # enlarge the 20px box the chevron is centred in.
            "box-shadow": "0 0 0 1px #c2c2c2, 0 1px 3px rgba(0,0,0,0.14)",
            "cursor": "pointer",
            "z-index": "20",
            "opacity": "0",
            "transition": "opacity 120ms ease",
            # Invisible has to mean non-interactive as well: on a window wide
            # enough to show every column the circles are still in the DOM, and
            # without this they would keep swallowing clicks on the cells under
            # them.
            "pointer-events": "none",
        },
        # One condition only: psc-visible says there are columns hidden that
        # way, measured in _MORE_HINT_INSTALL_JS. Deliberately NOT also gated
        # on hovering the table -- the hint exists for a reader who does not
        # know the table scrolls, and one they have to find by pointing at it
        # cannot tell them that.
        ".psc-scroll-hint.psc-visible": {
            "opacity": "1",
            "pointer-events": "auto",
        },
        # These are in the tab order while visible, so they need a focus ring,
        # and the disc already spends its box-shadow on the rim. An outline
        # draws outside the box and does not compete with it.
        ".psc-scroll-hint:focus-visible": {
            "outline": "2px solid #1f6feb",
            "outline-offset": "2px",
        },
        ".psc-more-cols": {"right": "6px"},
        ".psc-prev-cols": {"left": "6px"},
    }

    # height=None (domLayout: autoHeight) relies on the component calling back
    # into Streamlit.setFrameHeight() once ag-Grid's real layout is known, but
    # that callback fires before fitCellContents/suppressColumnVirtualisation
    # finish sizing rows, so the iframe gets stuck reporting a 0px height and
    # the whole table renders blank. Passing an explicit pixel height sized to
    # the row count sidesteps that broken callback while still avoiding
    # leftover empty space below a short table.
    # No buffer is added for the horizontal scroll bar itself: the
    # .ag-body-horizontal-scroll rule in GRID_CSS takes it out of flow on every
    # platform, so it never claims layout height. GRID_CHROME_HEIGHT covers what
    # is left -- ag-Grid's header overshoot and the gaps around that widget.
    grid_height = (
        GRID_HEADER_HEIGHT + GRID_ROW_HEIGHT * len(df) + GRID_CHROME_HEIGHT
    )
    # server_wins: the push-back below writes every accepted edit into session
    # state before rerunning, so state is always the source of truth and the
    # grid must follow it. The default client_wins ignores ALL server data
    # after the first edit, which left recomputed derived columns (and
    # rejected values snapping back) invisible until a full remount. Combined
    # with st_aggrid's auto row ids (::auto_unique_id::), server data lands as
    # a per-row delta -- no table reload, sort/scroll/column state preserved.
    # The context epoch handles the rejected-edit case: state is unchanged
    # there, so the df is byte-identical and nothing else would push the grid
    # a fresh props update to overwrite the bad on-screen cell.
    grid_options["context"] = {"sync_epoch": st.session_state.get("grid_epoch", 0)}
    grid_response = AgGrid(
        df[grid_cols],
        gridOptions=grid_options,
        height=grid_height,
        update_mode=GridUpdateMode.MODEL_CHANGED | GridUpdateMode.SELECTION_CHANGED,
        theme="balham",
        custom_css=GRID_CSS,
        allow_unsafe_jscode=True,
        show_toolbar=False,
        server_sync_strategy="server_wins",
        # st_aggrid identifies rows by ORDINAL, not by our position id: it sets
        # "::auto_unique_id::" to str(range(len(df))) and the frontend wires
        # getRowId to that column. Removing a row therefore renumbers every row
        # below it, and ag-grid keeps the old node id selected -- so the position
        # that slides into the freed slot inherits the checkbox and arms the
        # remove button against a row the user never picked. A custom getRowId
        # can't fix it (see the note above); remounting the component with a new
        # key is what actually drops the stale client-side selection.
        key=f"positions_grid_{st.session_state.get('grid_mount', 0)}",
    )

    # AgGrid's built-in download button renders inside the grid's iframe as a
    # floating overlay (position: absolute) -- it can't be moved outside the
    # frame via CSS, so this native button replaces it, sitting in normal page
    # flow right below the grid.
    export_df = df[display_cols].copy()
    two_decimal_cols = [c for c in [
        "Stop Price", "Position Size ($)", "Tranche Size ($)",
        "Entry Price ($)", "ATR Multiple", "ATR",
        "Stop Dist (%)", "Position Size (%)", "Tranche Size (%)",
    ] if c in export_df.columns]
    export_df[two_decimal_cols] = export_df[two_decimal_cols].round(2)
    # Applied to every column rather than a hand-listed text subset, so a
    # column added later can't quietly miss the guard. _csv_safe only touches
    # str cells, and after the rounding above the numeric columns hold floats,
    # so nothing numeric is altered.
    for _col in export_df.columns:
        export_df[_col] = export_df[_col].map(_csv_safe)
    caption_col, download_col = st.columns([9, 1], vertical_alignment="center")
    caption_col.caption(
        "✏️ Double-click an ATR Multiple or # Tranches cell to edit it — "
        "changes apply automatically. To delete a position, click the "
        "checkbox at the start of its row."
    )
    download_col.download_button(
        "⬇️ CSV", data=export_df.to_csv(index=False), file_name="positions.csv",
        mime="text/csv", help="Export to CSV",
    )

    # Push inline edits (ATR Multiple / # Tranches) back to state.
    # Matched by id, NOT positional index — sortable=True means the grid's row
    # order can differ from st.session_state.positions order at any time.
    # Rejection toasts collect here instead of firing inline: Streamlit
    # re-delivers the grid's last component value on EVERY rerun, so a
    # rejected payload is re-rejected on each unrelated interaction (risk
    # tweak, refresh, ...) until the next real grid event replaces it. The
    # fingerprint gate below toasts each distinct rejection once.
    rejection_msgs = []

    def _cell_value(raw, cast, fallback, label, symbol, minimum=None):
        # Grid cells are free-text editors, and auto-commit can now close an
        # editor mid-retype (same hazard as the sidebar fields): an emptied or
        # unparseable cell comes back as ""/None/NaN, which float()/int() would
        # either crash on or persist as NaN in positions.json. Keep the
        # position's previous value instead, mirroring _sync_portfolio_size --
        # including its toast: a silent revert renders another plausible number
        # and reads as "my edit applied" (review finding). Empty needs no toast
        # (the redraw showing the old value IS the outcome of clearing a cell).
        # NA-check before any == comparison: an emptied cell arrives as
        # pd.NA (not float NaN), and `pd.NA == ""` returns NA, whose bool()
        # raises "boolean value of NA is ambiguous" -- this crashed the whole
        # push-back (and the page) whenever a cell was cleared.
        if raw is None or (not isinstance(raw, str) and pd.isna(raw)) \
                or (isinstance(raw, str) and not raw.strip()):
            return fallback
        if isinstance(raw, str):
            # The grid displays toLocaleString values ("1,234.56"), so users
            # naturally type commas back in -- accept them like the Portfolio
            # Size field does.
            raw = raw.replace(",", "").strip()
            if not raw:
                return fallback
        try:
            value = cast(raw)
            # String "nan"/"inf" pass float() and the NaN check above only
            # catches float-typed NaN; int(float("inf")) raises OverflowError,
            # which the old except clause let crash the whole page.
            if not math.isfinite(value):
                raise ValueError(raw)
        except (TypeError, ValueError, OverflowError):
            rejection_msgs.append(
                f"{_md_escape(symbol)}: couldn't use '{_md_escape(raw)}' for "
                f"{label} — kept {fallback}")
            return fallback
        # Same floor the add-form widgets enforce (ATR_MULTIPLE_MIN, tranches
        # >= 1) -- the grid editor is the only other write path and accepted
        # 0/negatives, which just blank out every derived column.
        if minimum is not None and value < minimum:
            rejection_msgs.append(
                f"{_md_escape(symbol)}: {label} must be at least {minimum} — kept {fallback}")
            return fallback
        return value

    def _display_differs(raw, final):
        # Does the grid's client-side cell text disagree with the kept state
        # value? An emptied or rejected edit leaves the cell blank on screen
        # while state silently keeps the old number -- that split reads as
        # "my edit vanished/broke" until something repaints the grid.
        try:
            return float(str(raw).replace(",", "").strip()) != float(final)
        except (TypeError, ValueError):
            return True

    edited_df = grid_response["data"]
    if edited_df is not None:
        edited_by_id = {row["id"]: row for _, row in edited_df.iterrows()}
        changed = False
        resync = False
        for pos in st.session_state.positions:
            edited_row = edited_by_id.get(pos["id"])
            if edited_row is None:
                continue
            new_multiple = _cell_value(
                edited_row["ATR Multiple"], float, pos["atr_multiple"], "ATR Multiple",
                pos["symbol"], minimum=ATR_MULTIPLE_MIN)
            new_tranches = _cell_value(
                edited_row["# Tranches"], lambda v: int(float(v)), pos["tranches"], "# Tranches",
                pos["symbol"], minimum=1)
            if new_multiple != pos["atr_multiple"] or new_tranches != pos["tranches"]:
                pos["atr_multiple"] = new_multiple
                pos["tranches"] = new_tranches
                changed = True
            resync = resync or _display_differs(edited_row["ATR Multiple"], new_multiple) \
                or _display_differs(edited_row["# Tranches"], new_tranches)
        if rejection_msgs:
            # Toast each distinct rejected payload once. Replays of the same
            # stale payload on later reruns re-reject silently (the guarded
            # resync below still repaints the grid, so the display stays
            # right); a different bad value or a valid edit resets the gate.
            fingerprint = tuple(
                (pid, str(row["ATR Multiple"]), str(row["# Tranches"]))
                for pid, row in edited_by_id.items()
            )
            if st.session_state.get("last_rejection_fp") != fingerprint:
                st.session_state.last_rejection_fp = fingerprint
                for msg in rejection_msgs:
                    queue_toast(msg)
        else:
            st.session_state.pop("last_rejection_fp", None)
        if changed:
            save_state()
            # The derived columns (Stop Price, Position Size, ...) rendered by
            # THIS run were computed from pre-edit state -- the grid sits above
            # this push-back in the script. Without an immediate rerun the
            # on-screen numbers stay stale until the next unrelated
            # interaction, which is exactly the "editing is broken" report.
            # The rerun's changed dataframe reaches the live grid because the
            # component runs with server_sync_strategy="server_wins"; with the
            # default client_wins the grid ignores server data after the first
            # edit and these cells stayed blank/stale (verified live).
            st.session_state.grid_resync_pending = False
            st.rerun()
        if resync:
            if not st.session_state.get("grid_resync_pending"):
                # One guarded rerun only: grid_response replays the same stale
                # client data on the very next run, and an unguarded rerun
                # here would spin forever. The epoch bump changes
                # gridOptions.context so the component gets a fresh props
                # update (state itself is unchanged here, so the df alone
                # would be byte-identical) and server_wins overwrites the bad
                # cell in place -- no remount, sort and scroll survive.
                st.session_state.grid_resync_pending = True
                st.session_state.grid_epoch = st.session_state.get("grid_epoch", 0) + 1
                st.rerun()
            else:
                # This run IS the guarded rerun's stale replay: the client
                # repaint already happened, so consume the latch. Leaving it
                # set (the old behavior) meant the NEXT rejected edit found
                # the latch stuck and never got its repaint -- the cell sat
                # blank until a valid edit landed. No loop risk: reaching
                # here issues no rerun, so another cycle needs a real event.
                st.session_state.grid_resync_pending = False
        else:
            st.session_state.grid_resync_pending = False

    selected = grid_response.get("selected_rows")
    selected_ids = set(selected["id"]) if isinstance(selected, pd.DataFrame) and not selected.empty else set()
    if selected_ids and st.button(f"🗑️ Remove {len(selected_ids)} selected position(s)"):
        st.session_state.positions = [p for p in st.session_state.positions if p["id"] not in selected_ids]
        _clear_grid_selection()
        save_state()
        st.rerun()
