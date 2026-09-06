"""Symbol lookup and refresh. REQUIRES NETWORK -- talks to TradingView.

    python3 tests/test_lookup.py

Not run in CI: it depends on a third-party service that can be slow, rate
limited or briefly down, and a test that fails for those reasons teaches you
to ignore it. Run it by hand when touching the search or refresh paths.

Uses synthetic positions throughout -- it never reads a real positions.json.
"""
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lift import Checker, lift_app  # noqa: E402

LOOKUP_BUDGET_S = 2.0


def _position(symbol: str, exchange: str, entry_price: float) -> dict:
    """A row shaped exactly like add_position() builds one (app.py).

    Every key matters: refresh_all_positions() hard-indexes pos["id"] to map
    results back onto rows, so a partial fixture fails with a KeyError that
    looks like an app bug and is not one.
    """
    return {
        "id": uuid.uuid4().hex,
        "symbol": symbol,
        "asset_class": "Crypto",
        "exchange": exchange,
        "entry_price": entry_price,
        "atr_value": None,
        "display_name": None,
        "resolution_note": None,
        "fetch_error": None,
        "atr_multiple": 1.5,
        "tranches": 3,
        "added_at": "2026-01-01, 12:00:00 PM",
    }


# Liquid majors listed on every venue the app checks, so the test asserts on
# the app's behaviour rather than on which coins happen to be listed today.
def _sample() -> list[dict]:
    return [_position("BTCUSD", "BINANCE", 60000.0),
            _position("ETHUSD", "BINANCE", 3000.0)]


class _Session(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self[k] = v


class _FakeSt:
    def __init__(self, positions):
        self.session_state = _Session(positions=positions, timeframe_label="Daily")


def main() -> int:
    check = Checker()
    with tempfile.TemporaryDirectory() as tmp:
        ns = lift_app(tmp)
        ns["save_state"] = lambda: None

        print("1. A ticker and a full name both resolve")
        for query, kind in [("BTC", "ticker"), ("bitcoin", "full name"),
                            ("SOL", "ticker"), ("solana", "full name"),
                            ("ethereum", "full name")]:
            started = time.perf_counter()
            hits = ns["search_candidates"](query, "Crypto")
            secs = time.perf_counter() - started
            top = f", top={hits[0]['symbol']}@{hits[0]['exchange']}" if hits else ""
            check(f"{query!r} ({kind}) -> {len(hits)} hit(s){top}", bool(hits))
            if hits:
                check(f"    within {LOOKUP_BUDGET_S}s budget",
                      secs < LOOKUP_BUDGET_S, f"{secs:.2f}s")

        print("\n2. Exchange priority: Binance, then Coinbase, then the rest")
        venues = [c.get("exchange") for c in ns["search_candidates"]("bitcoin", "Crypto")]
        print(f"   order: {venues[:6]}")
        if "BINANCE" in venues and "COINBASE" in venues:
            check("BINANCE outranks COINBASE",
                  venues.index("BINANCE") < venues.index("COINBASE"))
        else:
            check("BINANCE is top venue", venues[:1] == ["BINANCE"], f"got {venues[:1]}")

        print("\n3. Refresh makes ONE request per position, not one per exchange")
        calls = []
        real_fetch_one = ns["_fetch_one"]

        def counting(symbol, exchange, timeframe, deadline=None):
            calls.append((symbol, exchange))
            return real_fetch_one(symbol, exchange, timeframe, deadline)

        ns["_fetch_one"] = counting

        positions = _sample()
        ns["st"] = _FakeSt(positions)
        ns["fetch_atr"].clear()
        calls.clear()
        started = time.perf_counter()
        ns["refresh_all_positions"]()
        secs = time.perf_counter() - started
        print(f"   {secs:.2f}s, {len(calls)} request(s): {calls}")
        check.equals("one request per position", len(calls), len(positions))
        check("every position got an ATR",
              all(p.get("atr_value") is not None and p.get("fetch_error") is None
                  for p in positions))
        check.equals("stored exchanges unchanged",
                     [p["exchange"] for p in positions], ["BINANCE", "BINANCE"])

        print("\n4. A WRONG stored exchange still resolves (no loss of reach)")
        positions = _sample()
        for p in positions:
            p["exchange"] = "NASDAQ"          # a stock venue for a crypto symbol
        ns["st"] = _FakeSt(positions)
        ns["fetch_atr"].clear()
        calls.clear()
        ns["refresh_all_positions"]()
        check("fell back to the fan-out", len(calls) > len(positions))
        check("still resolved every position",
              all(p.get("atr_value") is not None for p in positions))
        check("nobody left on the stock venue",
              all(p["exchange"] != "NASDAQ" for p in positions),
              f"exchanges now {[p['exchange'] for p in positions]}")

        print("\n5. A stored exchange of None (rows saved before the fix) works")
        positions = _sample()
        for p in positions:
            p["exchange"] = None
        ns["st"] = _FakeSt(positions)
        ns["fetch_atr"].clear()
        calls.clear()
        ns["refresh_all_positions"]()
        check("resolved without a stored venue",
              all(p.get("atr_value") is not None for p in positions))
        check("used the fan-out", len(calls) > len(positions))

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
