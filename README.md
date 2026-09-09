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

**You do not need to install Python, or anything else, first.** Get the files,
double-click — the start file sorts out everything it needs, including Python
itself if your computer does not already have it. Nothing is installed
system-wide, nothing asks for your password, and nothing changes your computer's
settings: it all stays inside the app's own folder. (One place needs a terminal:
on Linux the start file is a command rather than a double-click. It is in
step 3.)

**1. Get the files**

- **Mac** — install **GitHub Desktop** from <https://desktop.github.com>, open
  it, and skip the sign-in if it offers one — a public app needs no account.
  Choose **File → Clone Repository…**, click the **URL** tab, paste
  `https://github.com/sokodm/position-size-calculator` and click **Clone**.

  Use this rather than Download ZIP. macOS refuses to open files that came out
  of a downloaded ZIP and gives you no way to click past it; files arriving
  through GitHub Desktop are never flagged, so the app simply opens. Updating
  later is one click, and it leaves your saved positions alone.

- **Windows** and **Linux** — go to the app's page on GitHub:
  <https://github.com/sokodm/position-size-calculator>. Click the green **Code**
  button near the top right, then **Download ZIP**.

**2. Unzip** — only if you downloaded a ZIP; GitHub Desktop hands Mac a folder
that is ready to use.

- **Mac** — double-click the ZIP file. macOS will then block the start file, so
  read [Mac: if you used Download ZIP](#mac-if-you-used-download-zip) before
  step 3.
- **Windows** — right-click the ZIP file → **Extract All…** → **Extract**.
  Do not skip this: double-clicking a ZIP on Windows only previews it, and the
  app cannot run from a preview.
- **Linux** — double-click the ZIP file, or run
  `unzip position-size-calculator-main.zip` in a terminal.

**3. Start it** — open the app's folder and start the file for your system:

- **Mac** — `Start Calculator (Mac).command`

  GitHub Desktop puts the folder in `Documents/GitHub/position-size-calculator`
  unless you picked somewhere else. Double-click the start file; nothing blocks
  it. (If you took the ZIP route anyway, see [Mac: if you used Download
  ZIP](#mac-if-you-used-download-zip) below.)

- **Windows** — `Start Calculator (Windows).bat`
- **Linux** — open a terminal in the folder and run:

  ```sh
  ./"Start Calculator (Linux).sh"
  ```

  Double-clicking it does work on some Linux desktops, but many open it in a
  text editor instead. The command above always works.

The first start takes a few minutes while it downloads and installs what it
needs; after that it opens in seconds. Your browser opens automatically. The app
then keeps running on its own, so the terminal window can be closed — and
starting it again just reopens the tab rather than starting a second copy.

To stop it: quit `python` from Activity Monitor on Mac, from the Task Manager's
**Details** tab on Windows, or run `pkill -f streamlit` on Linux.

If your computer has no Python, the start file says so and fetches its own
private copy (24 MB on Mac, 45 MB on Windows, 33 MB on Linux) into the folder
before carrying on. You do not have to do anything — it is just why the
very first start can take a little longer.

### Mac: if you used Download ZIP

The ZIP still works, but macOS will refuse to open the start file the first
time: *"Apple could not verify 'Start Calculator (Mac).command' is free of
malware."* That is not about this app — macOS flags every file that came out of
a downloaded ZIP, whoever wrote it, and the dialog deliberately offers no way
through. Cloning with GitHub Desktop (step 1) avoids it entirely.

Click **Done** — do **not** click *Move to Trash* — then:

1. Open **System Settings → Privacy & Security**.
2. Scroll down to **Security**. A line names the blocked file, with an **Open
   Anyway** button beside it.
3. Click **Open Anyway**, then confirm.

That button only appears for a short while after the block, so do it straight
away; if it has gone, double-click the start file again to bring it back. The
first time it runs, the start file clears the flag from the whole folder, so
this is a one-time thing rather than something you repeat.

Right-click → **Open** was the old fix for this. macOS 15 (Sequoia) removed it,
which is why it no longer does anything.

### If it does not start

| Message | Fix |
|---|---|
| Mac: "Apple could not verify… is free of malware", or "unidentified developer" | The files came out of a downloaded ZIP. Click **Done** — *not* **Move to Trash** — then follow [Mac: if you used Download ZIP](#mac-if-you-used-download-zip). Getting the files with GitHub Desktop instead (step 1) avoids the block altogether. |
| Windows: "Windows protected your PC" | Click **More info** → **Run anyway**. Once only. |
| Window opens and closes without the browser opening | On Windows the window closing is normal once the app has started — this row is about it closing with no browser tab. Usually the app is being run from inside the ZIP preview: extract the folder properly (step 2). If it is extracted and still closes, see **Windows: getting a diagnosis** below. |
| Anything about Python failing to download | Check your internet connection and try again. If it keeps failing, install Python by hand — see [Installing Python](#installing-python). |

### Windows: getting a diagnosis

When the window closes too fast to read, double-click **`Diagnose (Windows).bat`**
instead. It runs the start file as a *separate* process, so it survives even a
start file that is killed outright, and writes a report to `logs/` that opens in
your browser: what Python the PC has, whether the folder was extracted properly,
whether the download can reach the internet, and the full text of any error.

The start file also keeps a running log of its own steps in
`logs\launcher-steps.log`, written as each step happens — so the last line tells
you where it got to even when the window is gone. Neither file leaves your
computer; both are ignored by Git.

### Linux: if it does not start

| Message | Fix |
|---|---|
| Double-clicking the start file opens it in a text editor | That is your desktop's setting for executable text files, not a fault. Start it from a terminal instead — step 3. |
| `Permission denied` | Your unzip tool dropped the file's executable flag. Run `chmod +x "Start Calculator (Linux).sh"` once, then start it again. |
| It downloads its own Python even though `python3` is installed | Expected on Debian and Ubuntu, where `python3` ships without the `venv` piece the app needs. It fetches its own copy rather than fail halfway through. Nothing to fix — or install `python3-venv` if you would rather it used yours. |
| "not one we have a Python download for", or a message about musl | Your machine is not x86-64 or ARM64, or it uses musl rather than glibc (Alpine). Install Python from your package manager — see [Installing Python](#installing-python). |

### Installing Python

**Most people can skip this.** The start file installs Python for you. These
steps are the fallback for when that download cannot work — no internet on the
machine, a network that blocks the download, or a workplace computer locked
down. It is free and takes about two minutes.

**Mac**

1. Go to <https://www.python.org/downloads/> and click the yellow **Download
   Python** button.
2. Open the downloaded `.pkg` file from your Downloads folder.
3. Click **Continue** through the installer, **Agree** to the licence, then
   **Install**. Enter your Mac password when asked.
4. Close the installer and double-click `Start Calculator (Mac).command` again.

**Windows**

1. Go to <https://www.python.org/downloads/> and click the yellow **Download
   Python** button.
2. Open the downloaded `.exe` file from your Downloads folder.
3. **Before clicking anything else, tick the "Add python.exe to PATH" box at the
   bottom of that first screen.** This is the step people miss, and without it
   Windows cannot find Python afterwards.
4. Click **Install Now** and wait for it to finish.
5. Close the installer and double-click `Start Calculator (Windows).bat` again.

**Linux**

Use your distribution's package manager. On Debian or Ubuntu the `venv` package
matters as much as Python itself — without it the app cannot build its private
environment, and will download its own Python instead:

```sh
sudo apt install python3 python3-venv     # Debian, Ubuntu, Mint
sudo dnf install python3                  # Fedora
sudo pacman -S python                     # Arch
```

Then run `./"Start Calculator (Linux).sh"` again.

### Advanced: install with Git

Unlike the double-click route, **this one needs Python 3.10–3.13 already
installed** (3.14 is not supported yet — one of the pinned dependencies caps
there, and the start files download a private 3.12 rather than use it) —
`run.py` is itself a Python program, so it cannot fetch the thing that runs it.
That bootstrap lives in the three start files.

```sh
git clone https://github.com/sokodm/position-size-calculator.git
cd position-size-calculator
```

Then, on **Mac** or **Linux**:

```sh
python3 run.py
```

or on **Windows**:

```bat
py -3 run.py
```

Identical result — the start files just find a Python and call `run.py` for you.

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

If your machine had no Python, there is also a `.runtime` folder holding the
private copy the start file downloaded — a [python-build-standalone][pbs] build
of CPython 3.12, checked against a SHA-256 hash pinned in the start file before
it is unpacked. It is gitignored and specific to one OS and processor, same as
`.venv`. Deleting either folder is safe; the next start rebuilds it.

[pbs]: https://github.com/astral-sh/python-build-standalone

To remove the app completely, delete the folder. Nothing lives outside it.

## Tests

Plain scripts, no test framework to install:

```sh
python3 tests/test_state.py     # state, validation, sizing maths. No network.
python3 tests/test_lookup.py    # symbol search and refresh. Needs network.
```

`test_state.py` runs in CI on macOS, Windows and Linux — the three platforms the
launchers target — against Python 3.10 (the floor this project supports) and
3.12. `test_lookup.py` calls TradingView and
is run by hand. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how the tests reach into `app.py`
without a Streamlit runtime.

## License

[MIT](LICENSE).
