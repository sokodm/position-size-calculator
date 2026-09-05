# Position Size Calculator

Works out how big a position to take, from your portfolio size, the risk you
accept per trade, and the instrument's ATR. Runs entirely on your own machine.

## Running it

You need **Python 3.10 or newer**. Nothing else — the first launch installs
what it needs into a `.venv` folder next to these files, and later launches
skip straight to opening the app.

**macOS** — double-click `Start Calculator (Mac).command`

> The first time, macOS may say the file "cannot be opened because it is from an
> unidentified developer". **Right-click the file → Open → Open.** You only do
> this once. If it says "permission denied" instead, the executable flag was
> stripped in transit; open Terminal in this folder and run
> `chmod +x "Start Calculator (Mac).command"`.

**Windows** — double-click `Start Calculator (Windows).bat`

> If you see "Python is not recognized", install Python from
> <https://www.python.org/downloads/> and **tick "Add python.exe to PATH"** on
> the installer's first screen.

Either way a terminal window opens, then your browser. **Leave the terminal
window open while you use the app** — closing it stops the app.

To run it by hand instead: `python3 run.py` (macOS) or `py -3 run.py` (Windows).

## Your data

Everything stays on your computer.

- **Your positions** live in `positions.json`, in this folder. Nothing is
  uploaded, and there is no account or login. Back up that one file and you
  have backed up your portfolio; delete it and you start empty.
- **Portfolio size, risk percentage and entry prices never leave the machine.**
  The only thing sent to TradingView is a symbol, an exchange and a timeframe,
  in order to fetch prices and ATR.
- **Prices are cached in memory for 60 seconds** and vanish when you close the
  app. Nothing is cached to disk.

Because the portfolio is a plain file in this folder, **each person needs their
own copy of the folder**. Two people cannot share one installation — they would
share one portfolio and overwrite each other's saves.

## Sharing it with someone else

Send them a zip of this folder **excluding your own data and local files**:

```sh
# macOS / Linux — run from inside this folder
zip -r ~/Desktop/position-size-calculator.zip . \
    -x 'positions.json' 'streamlit.log' '.venv/*' '__pycache__/*' '.DS_Store'
```

```powershell
# Windows PowerShell — run from inside this folder
Get-ChildItem -Force |
  Where-Object { $_.Name -notin @('positions.json','streamlit.log','.venv','__pycache__') } |
  Compress-Archive -DestinationPath "$HOME\Desktop\position-size-calculator.zip"
```

Those four exclusions matter:

| Excluded | Why |
|---|---|
| `positions.json` | your live portfolio — sizes, entry prices, risk settings |
| `streamlit.log` | may contain your portfolio size in plain text |
| `.venv/` | ~200 MB, and built for *your* OS — it will not work on theirs |
| `__pycache__/` | stale compiled bytecode |

Share the zip through a file share (OneDrive, SharePoint, Google Drive) rather
than as an email attachment — most mail systems strip `.bat` files, which would
leave Windows users without a launcher.
