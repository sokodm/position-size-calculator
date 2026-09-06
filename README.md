# Position Size Calculator

Auto-calculated ATR sets your stop price and position size — capping your loss
on any trade at the risk % you set.

> **Not financial advice.** This is a calculator, not a recommendation. It tells
> you what a position size *would* be under the risk rule you gave it; it has no
> opinion on whether the trade is a good one. Market data comes from a
> third-party source and may be delayed, wrong, or missing. You are responsible
> for every order you place. The software comes with no warranty of any kind —
> see [LICENSE](LICENSE).

![The calculator with three crypto positions loaded. Portfolio size and risk
percentage sit in the left sidebar; the highlighted Stop Price, Position Size
and Tranche Size columns are the calculated outputs, and the columns to their
right show the inputs and the ATR they were derived from.](docs/screenshot.png)

## How it works

- **ATR (Average True Range, 14-period)** — TradingView's average of each bar's
  true trading range on the selected timeframe: Hourly, Daily, and Weekly
  measure typical hour-, day-, or week-sized moves.
- **Stop distance = ATR × ATR multiple**; stop price = entry − stop distance.
- **Position size** is set so a stop-out loses exactly your Risk % of the
  portfolio, then split across # Tranches.
- **Live data** — price & ATR are pulled from TradingView when a position is
  added and on every refresh.
- **Global settings apply instantly** — Portfolio Size and Risk (%) re-compute
  every saved position on the spot; switching the ATR timeframe automatically
  re-pulls live price & ATR for all saved positions.

## What it works out

You give it four things per position — entry price, ATR multiple, number of
tranches, and the symbol — plus two settings that apply to everything: your
portfolio size and the percentage of it you are willing to lose on one trade.
It fetches the instrument's ATR and computes:

```
risk budget  (R) = portfolio size x risk %
stop distance    = ATR x ATR multiple
stop price       = entry price - stop distance
position size $  = R / (stop distance / entry price)
tranche size $   = position size / number of tranches
```

The useful part is the divisor. Because the stop distance is expressed as a
*fraction of entry*, position size scales inversely with volatility: a wide-ATR
instrument automatically gets a smaller position for the same dollar risk, so
every open position risks the same amount even though their charts do not look
alike.

If the ATR multiple is wide enough to put the stop at or below zero, the app
says so instead of quietly reporting a size derived from an unplaceable stop.

## Getting it

```sh
git clone https://github.com/sokodm/position-size-calculator.git
cd position-size-calculator
```

You need **Python 3.10 or newer**. Nothing else — the first launch installs what
it needs into a `.venv` folder next to these files, and later launches skip
straight to opening the app.

## Running it

**macOS** — double-click `Start Calculator (Mac).command`

> The first time, macOS may say the file "cannot be opened because it is from an
> unidentified developer". **Right-click the file → Open → Open.** You only do
> this once. If it says "permission denied" instead, the executable flag was
> lost — that happens when the folder arrives as a downloaded ZIP rather than a
> `git clone`. Open Terminal in this folder and run
> `chmod +x "Start Calculator (Mac).command"`.

**Windows** — double-click `Start Calculator (Windows).bat`

> If you see "Python is not recognized", install Python from
> <https://www.python.org/downloads/> and **tick "Add python.exe to PATH"** on
> the installer's first screen.

Either way a terminal window opens, then your browser. **Leave the terminal
window open while you use the app** — closing it stops the app.

To run it by hand instead: `python3 run.py` (macOS) or `py -3 run.py` (Windows).

## First run

The app starts deliberately empty, so nothing is ever computed from someone
else's numbers:

- **Portfolio Size is blank.** Until you set it, the calculated columns stay
  empty — that is not a bug. Risk defaults to 1%.
- **Add a position by searching, not typing.** Enter a ticker *or* a full name
  ("SOL" or "solana", "BTC" or "bitcoin") and press **Search**. The app resolves
  which exchange actually lists it before adding the row, which is what lets the
  ATR lookup succeed later.
- **Your first search is the slow one.** It may check several exchanges;
  after that, results are cached for 60 seconds.

## Your data

Everything stays on your computer.

- **Your positions** live in `positions.json`, in this folder. Nothing is
  uploaded, and there is no account or login. Back up that one file and you have
  backed up your portfolio; delete it and you start empty. It is listed in
  `.gitignore`, so it will not follow you into a commit.
- **Portfolio size, risk percentage and entry prices never leave the machine.**
  The only thing sent out is a symbol, an exchange and a timeframe, in order to
  fetch prices and ATR.
- **Prices are cached in memory for 60 seconds** and vanish when you close the
  app. Nothing is cached to disk.

Because the portfolio is a plain file in this folder, **each person needs their
own copy**. Two people cannot share one installation — they would share one
portfolio and overwrite each other's saves.

## Where the prices come from

Market data comes from **TradingView**, via the `tradingview-mcp-server` package
and TradingView's public symbol-search endpoint. That endpoint is not a
documented, supported API: it can change or start refusing requests without
notice, and heavy automated use may fall outside TradingView's terms of service.
This app makes a handful of requests per lookup and caches for 60 seconds, which
is well within normal interactive use, but if you fork it and remove the caching,
that is on you.

There is no API key to configure, and no data provider account is needed.

## What gets installed

Dependencies are pinned in `requirements.txt` and installed into a local
`.venv` — nothing is installed system-wide.

| Package | Why |
|---|---|
| `streamlit==1.62.0` | the UI. Pinned exactly: the app's CSS targets this version's DOM, so a newer Streamlit can shift the layout |
| `streamlit-aggrid` | the editable positions table |
| `streamlit-extras` | UI components Streamlit core handles poorly |
| `tradingview-mcp-server` | price and ATR lookups |
| `pip-system-certs` | makes Python trust the operating system's certificate store |

That last one is only needed behind a corporate proxy that inspects TLS traffic
(Zscaler, Netskope and similar), where Python's bundled certificate list rejects
the proxy's certificate and every price lookup fails. On a home network it is
harmless but unnecessary — remove that line from `requirements.txt` if you would
rather not have it.

The `.venv` folder is roughly **700 MB** and is built for the OS that created
it. It is gitignored, and copying it between machines will not work — let each
machine build its own on first launch.

Most of that size is not this app. `streamlit-extras` pulls in around 120
transitive packages, including a Snowflake connector, the AWS SDK, matplotlib
and plotly — none of which this calculator uses directly. That is worth knowing
before you install it: it is a large surface for a local tool. `pip install
--dry-run -r requirements.txt` prints the full tree if you want to read it
first.

## Tests

Plain scripts, no test framework to install:

```sh
python3 tests/test_state.py     # state, validation, sizing maths. No network.
python3 tests/test_lookup.py    # symbol search and refresh. Needs network.
```

`test_state.py` runs in CI on macOS and Windows — the two platforms the
launchers target — against Python 3.10 (the floor this project supports) and
3.12. `test_lookup.py` calls TradingView and
is run by hand. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how the tests reach into `app.py`
without a Streamlit runtime.

## License

[MIT](LICENSE).
