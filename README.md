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

## Install it

No command line, no Git account, nothing to sign up for. Three steps.

### 1. Download the ZIP

Open <https://github.com/sokodm/position-size-calculator>, click the green
**Code** button near the top right, then click **Download ZIP**. A file called
`position-size-calculator-main.zip` lands in your **Downloads** folder.

### 2. Unzip it

A ZIP is a whole folder squashed into a single file. Unzipping unpacks it again —
your computer already knows how, there is nothing extra to install.

- **Mac** — double-click the `.zip` file. A folder named
  `position-size-calculator-main` appears right next to it. Done.
- **Windows** — **right-click** the `.zip` file → **Extract All…** → **Extract**.
  A folder named `position-size-calculator-main` appears.

> **Windows: do not skip the extracting.** Double-clicking a ZIP on Windows only
> shows you what is inside — it looks like a folder, but it is a preview, and the
> app cannot run from it. You need the real folder that **Extract All** creates.

Move that folder anywhere you like — Documents, Desktop, wherever. You can rename
it. The only rule is that the files inside stay together.

### 3. Double-click the start file

Open the folder and double-click:

- **Mac** — `Start Calculator (Mac).command`
- **Windows** — `Start Calculator (Windows).bat`

A plain black text window opens. The first time, it spends a few minutes
installing what the app needs — that is normal, and it only happens once. Then
your browser opens with the calculator in it.

**Leave the black window open while you use the app** — closing it stops the app.
To use the calculator again another day, double-click the same file; after the
first time it starts in seconds.

### If it does not start

**It says Python is not installed.** Python is the free programming language this
calculator is written in, and most Macs and PCs do not come with it.

1. Go to <https://www.python.org/downloads/> and click the big **Download
   Python** button.
2. Open the file it downloads and click through the installer.
3. **Windows only, and this step matters:** on the installer's *first* screen,
   tick **Add python.exe to PATH** before clicking Install. Skip it and Windows
   will not be able to find Python afterwards.
4. Double-click the start file again.

**Mac says the file "cannot be opened because it is from an unidentified
developer".** macOS blocks anything downloaded from the internet until you vouch
for it once. **Right-click** `Start Calculator (Mac).command` → **Open** →
**Open** in the box that appears. You only ever do this once.

**Windows shows a blue "Windows protected your PC" box.** Click **More info**,
then **Run anyway**. Same idea — Windows is cautious about files from the
internet.

**The window flashes open and shuts immediately.** Both start files are written
to stay open and print the reason they stopped, so this almost always means the
app is being run from inside the ZIP preview rather than a properly extracted
folder. Go back to step 2.

*Comfortable with a terminal?* `git clone
https://github.com/sokodm/position-size-calculator.git` and then `python3 run.py`
(Mac) or `py -3 run.py` (Windows) does exactly the same thing. Python 3.10 or
newer is the only requirement either way; everything else installs itself into a
`.venv` folder beside the app.

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
| `tradingview-mcp-server` | price and ATR lookups |
| `pip-system-certs` | makes Python trust the operating system's certificate store |

That last one is only needed behind a corporate proxy that inspects TLS traffic
(Zscaler, Netskope and similar), where Python's bundled certificate list rejects
the proxy's certificate and every price lookup fails. On a home network it is
harmless but unnecessary — remove that line from `requirements.txt` if you would
rather not have it.

The `.venv` folder is roughly **435 MB** and is built for the OS that created
it. It is gitignored, and copying it between machines will not work — let each
machine build its own on first launch.

Most of that size is not this app: `pyarrow`, `pandas` and `numpy` alone are
about 220 MB, and Streamlit requires all three. `pip install --dry-run -r
requirements.txt` prints the full tree if you want to read it first.

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
