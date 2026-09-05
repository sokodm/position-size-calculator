"""State loading and the sizing maths. No network -- safe to run anywhere.

    python3 tests/test_state.py

Covers the two things most likely to break quietly:

1. A fresh install must start with NO portfolio size, without the app
   mistaking that empty state for a corrupt file. Those two look identical on
   disk (both are `portfolio_size: 0.0`) and telling them apart wrong either
   scares a new user with a corruption banner or silently swallows real
   damage.
2. compute_outputs() must refuse to invent numbers from incomplete input
   rather than returning a plausible-looking size.
"""
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lift import Checker, lift_app, lift_extra  # noqa: E402


def main() -> int:
    check = Checker()
    with tempfile.TemporaryDirectory() as tmp:
        ns = lift_app(tmp)
        lift_extra(ns, "compute_outputs")
        data_file = Path(ns["DATA_FILE"])

        def write(**payload):
            base = {"risk_pct": 1.0, "timeframe_label": "Weekly",
                    "positions": [], "revision": 1}
            base.update(payload)
            import json
            data_file.write_text(json.dumps(base))

        def clear():
            for stale in data_file.parent.glob("positions.json*"):
                stale.unlink()

        print("1. A fresh install starts with no portfolio size")
        check.equals("DEFAULT_STATE ships 0", ns["DEFAULT_STATE"]["portfolio_size"], 0.0)
        check.equals("no positions.json on disk", data_file.exists(), False)
        state = ns["load_state"]()
        check.equals("loads as unset", state["portfolio_size"], 0.0)
        check.equals("no scary load_error", state.get("load_error"), None)
        check.equals("risk defaults to 1%", state["risk_pct"], 1.0)
        check.equals("field renders EMPTY, not '0'", ns["_money_field"](state["portfolio_size"]), "")

        print("\n2. An unset value saved to disk is NOT corruption")
        write(portfolio_size=0.0)
        state = ns["load_state"]()
        check.equals("still unset after a round-trip", state["portfolio_size"], 0.0)
        check.equals("no load_error banner", state.get("load_error"), None)
        check.equals("no .invalid- backup written",
                     list(data_file.parent.glob("positions.json.invalid-*")), [])
        clear()

        print("\n3. Genuinely broken values ARE still reported")
        for bad in (-5000.0, float("nan"), float("inf"), "50000", True, None):
            write(portfolio_size=bad)
            state = ns["load_state"]()
            label = "nan" if isinstance(bad, float) and math.isnan(bad) else repr(bad)
            check.equals(f"{label} flagged and reset",
                         (bool(state.get("load_error")), state["portfolio_size"] == 0.0),
                         (True, True))
            clear()

        print("\n4. A real saved portfolio is untouched")
        write(portfolio_size=500000.0, risk_pct=1.5, timeframe_label="Daily", revision=3)
        state = ns["load_state"]()
        check.equals("value preserved", state["portfolio_size"], 500000.0)
        check.equals("no load_error", state.get("load_error"), None)
        check.equals("shown formatted", ns["_money_field"](state["portfolio_size"]), "500,000")
        clear()

        print("\n5. _money_field boundaries")
        check.equals("0 -> empty", ns["_money_field"](0.0), "")
        check.equals("negative -> empty", ns["_money_field"](-1.0), "")
        check.equals("cents preserved", ns["_money_field"](1234.56), "1,234.56")
        check.equals("whole dollars", ns["_money_field"](1234.0), "1,234")

        print("\n6. compute_outputs refuses to invent numbers")
        compute = ns["compute_outputs"]
        check.equals("blank while portfolio unset",
                     compute(entry_price=100.0, atr_value=2.0, atr_multiple=1.5,
                             tranches=1, portfolio_size=0.0, risk_pct=1.0), None)
        out = compute(entry_price=100.0, atr_value=2.0, atr_multiple=1.5,
                      tranches=1, portfolio_size=500000.0, risk_pct=1.0)
        check("computes once set", out is not None)

        print("\n7. The sizing identity holds")
        # 500k at 1% = 5,000 of risk. ATR 2.0 x 1.5 = 3.0 stop distance on a
        # 100.0 entry = 3%. 5,000 / 0.03 = 166,666.67.
        out = compute(entry_price=100.0, atr_value=2.0, atr_multiple=1.5,
                      tranches=2, portfolio_size=500_000.0, risk_pct=1.0)
        check.equals("risk budget", round(out["r_dollars"], 2), 5000.0)
        check.equals("stop distance", round(out["stop_distance_dollars"], 2), 3.0)
        check.equals("stop price", round(out["stop_price"], 2), 97.0)
        check.equals("position size", round(out["position_size_dollars"], 2), 166666.67)
        check.equals("tranche halves it", round(out["tranche_size_dollars"], 2), 83333.33)
        check("stop is reachable", out["stop_reachable"])

        print("\n8. An unreachable stop is flagged, not silently sized")
        # A 60x multiple on a 2.0 ATR puts the stop 120 below a 100 entry.
        out = compute(entry_price=100.0, atr_value=2.0, atr_multiple=60.0,
                      tranches=1, portfolio_size=500_000.0, risk_pct=1.0)
        check("row still returned (diagnostics stay on screen)", out is not None)
        check.equals("stop_reachable is False", out["stop_reachable"], False)

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
