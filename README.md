# ICT Power Of Three → MEXC → Telegram signal scanner

A **signal-only** scanner. It ports the signal logic of the TradingView indicator
*"ICT Power Of Three | Flux Charts"* (Pine Script v5, © fluxchart, MPL-2.0) to Python, scans the
top-150 MEXC USDT-M perpetual Futures pairs by 24-hour turnover on **1h closed candles only**, and sends **BUY / SELL** alerts to
Telegram. It runs on GitHub Actions hourly, five minutes after each UTC hour.

> **It never places orders.** It only reads MEXC *public Futures* market data (no MEXC API key) and calls
> the Telegram Bot API. Not financial advice.

## Files

| File | Purpose |
|---|---|
| `po3_strategy.py` | Pine → Python port: ATR, accumulation → breakout → manipulation → entry, TP/SL simulation |
| `mexc_data.py` | MEXC public Futures API client, top-150 USDT-M perpetual contract selection, closed-candle filter |
| `telegram_bot.py` | Credentials from env vars, message formatting, safe sending (secrets never logged) |
| `scanner.py` | Orchestration, CLI, freshness + duplicate logic |
| `state_store.py` | Persistent sent-signal registry (atomic JSON) |
| `config.py`, `models.py` | Scanner settings (env-overridable), data classes |
| `scripts/state_branch.sh` | Persists the registry on a `scanner-state` branch between Actions runs |
| `.github/workflows/scanner.yml` | Scheduled (hourly) + manual (`workflow_dispatch`) run, Python 3.11 |
| `.github/workflows/tests.yml` | Runs the test suite on push / PR (Python 3.11 & 3.12) |
| `tests/` | 69 tests (unit + local-HTTP-server end-to-end) |

## Setup

### 1. Create the Telegram bot and find your chat ID
1. In Telegram open **@BotFather** → `/newbot` → copy the **token** (treat it like a password; never paste it in chats, issues or code).
2. Send any message to your new bot (or add it to your group/channel and post once).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read `message.chat.id`
   (groups/channels are negative numbers, e.g. `-100123...`). For a channel the bot must be an admin.

### 2. Put the project in GitHub
Web UI: create a new repository (see the *Public vs private* note below), then **Add file → Upload files**,
drag in the project contents (keep the folder structure; make sure `.github/workflows/` and `scripts/` are included)
and **Commit changes** to `main`.

Or with git:
```bash
cd po3-mexc-scanner
git init -b main
git add .
git commit -m "Add PO3 MEXC scanner"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 3. Add the two repository secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | chat id from step 1 |

### 4. Allow the workflow to push the state branch
Repo → **Settings → Actions → General → Workflow permissions → "Read and write permissions"** → Save.
(The workflow also declares `permissions: contents: write`, but an org/repo default of read-only can override it.)

### 5. Enable and test
1. **Actions** tab → enable workflows if prompted.
2. Run **PO3 Signal Scanner → Run workflow** with **telegram_test = true** → you should get a test message in Telegram.
3. Run it again with **dry_run = true** → signals (if any) are printed in the log, nothing is sent, no state is stored.
4. Run it with defaults, or just wait: it runs hourly, five minutes after each UTC hour (see limitations).

### Local usage
```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # run tests
python scanner.py --dry-run               # no secrets needed
python scanner.py --dry-run --symbols BTC_USDT,ETH_USDT --timeframes 15m
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python scanner.py --telegram-test
python scanner.py
```
If either Telegram variable is missing the scanner exits with code 2 and prints only the *names* of the missing variables.

## What a signal looks like
`BUY SIGNAL — BTC_USDT (15m)`, strategy name, Entry, Stop Loss (± %), Take Profit (± %), Risk/Reward,
signal-candle open and close time (UTC).

**Risk/Reward** follows the Pine script: default `RR = 0.86`, i.e. TP distance = 0.86 × SL distance
(reported as `1 : 0.86`). The SL is `5 × ATR(50)` away (risk = "Normal"), so by default risk is larger than reward —
that is the original indicator's default, not a choice made here.

## How the port maps to the Pine script
Defaults are identical to the Pine inputs: Algorithm Mode *Small Manipulation* (accumulation length 40, max accumulation
size 5×ATR, min manipulation 0.6×ATR), Breakout *Wick*, TP/SL *Dynamic*, Risk *Normal* (SL = 5×ATR), RR 0.86, ATR length 50,
accumulation expansion 0.5×ATR, Fixed-mode 0.3 % TP / 0.4 % SL. All Pine inputs are available on `PO3Config`.

State machine (kept in Pine order, including **same-bar cascading** — accumulation, breakout, manipulation and entry can all occur in one candle, because the Pine blocks are sequential `if`s):
1. *Waiting For Accumulation*: `highest(40) − lowest(40) ≤ ATR × 5` → box = high/low ± 0.5×ATR.
2. *Accumulation End*: wick (or close) outside the box → *Waiting For Manipulation*.
3. *Manipulation*: price beyond box ± 0.6×ATR. **Sweep of highs → SELL; sweep of lows → BUY.**
4. Entry at that candle's **close**; SL/TP set by ATR (Dynamic) or % (Fixed).
5. Exit simulation on later candles (TP first, then SL; if both hit in one candle **SL wins**, as in Pine). A new PO3 cycle only starts after the previous trade exited — exactly like Pine — which is why exits are simulated even though only entries are alerted.

## Pine-to-Python differences (read this)
**The port has been unit-tested against hand-computed values, but it has NOT been compared with live TradingView output.** Do not assume exact equivalence until you check it (procedure below). Known differences:

1. **History window.** TradingView runs the script on up to ~4800 bars of chart history (`maxDistanceToLastBar`); the scanner fetches the last **500** candles per symbol/timeframe. The state machine is path-dependent (it only resets after a trade exits), so the cycle in progress on the first fetched candle can differ from TradingView's, and a signal can appear/disappear until both sides resynchronise. Also, a 500-candle window slides each run.
2. **ATR seeding.** Pine's `ta.atr` is seeded over the whole chart history; here over the first 50 fetched candles. The difference decays (≈ 0.98ⁿ) and is negligible after a few hundred candles, but not zero.
3. **Data source.** Candles come from MEXC Futures public REST API. TradingView's MEXC feed should match, but candle boundaries/volume quirks or a different exchange selected on your chart will change results.
4. **Alert filter (added, not in Pine).** Pine alerts fire live on bar close. The scanner scans history each run, so it only alerts signals whose candle closed within a freshness window (default 420 min (7 hours) for 1h) **and** that have not already hit TP/SL. This is a delivery filter; it does not change PO3 rules.
5. **Not ported:** zones/boxes/labels, the backtest dashboard, and the Pine TP-hit / SL-hit alerts (you asked for BUY/SELL only). The exit logic is still simulated internally.
6. **Fixed-mode and floating point.** Same formulas as Pine, evaluated in IEEE double precision; results at exactly-equal boundaries could theoretically differ by 1 ulp.

### Verify against TradingView
```bash
python scanner.py --inspect BTC_USDT --timeframes 15m
```
prints every signal PO3 finds in the fetched window (candle time UTC, entry, SL, TP, outcome). Load the same symbol on
TradingView (MEXC feed, same timeframe, indicator defaults, chart timezone UTC) and compare the Buy/Sell labels
for recent candles. Differences near the *start* of the window are expected (difference 1); differences in the most recent days are worth investigating.

## Futures market-data endpoints

The scanner uses MEXC's public USDT-M perpetual Futures market-data endpoints on `https://api.mexc.com`:
`/api/v1/contract/ping`, `/api/v1/contract/detail`, `/api/v1/contract/ticker`, and
`/api/v1/contract/kline/{symbol}`. It only reads public data; no MEXC key is required and no orders are sent.
The symbol format is `BTC_USDT`. For convenience, CLI inputs such as `BTCUSDT` are normalized to `BTC_USDT`.

## Closed candles only
MEXC Futures returns candle arrays that may include the still-forming candle as the last candle. A candle is used only if `open + interval + 5 s ≤ MEXC server time`
(falls back to the runner clock if `/api/v1/contract/ping` fails). Indicators and the state machine never see a forming candle.

## Signal lookback and Telegram pacing

The scanner recomputes the PO3 strategy over fetched candle history, but only considers signals whose 1h candle closed within the last 7 hours. This provides a seven-candle recovery window after delayed/missed runs. Already-resolved trades (TP/SL reached on later candles) are still suppressed to avoid sending stale trade ideas. A signal ID is recorded after successful delivery so the same pair/candle/direction is not sent again. If several alerts are due in one scan, the scanner waits 15 seconds between send attempts.

## Duplicate prevention and missed runs
* Every signal has an ID: `PO3|SYMBOL|TIMEFRAME|DIRECTION|signal-candle-open-ms`.
* IDs are stored in `state/sent_signals.json`, persisted between runs on an orphan branch **`scanner-state`** (created automatically; `main` gets no runtime commits). Entries are pruned after 14 days.
* The ID is saved **only after Telegram confirms delivery**, and immediately after each message — a failed send is retried on the next run (while still fresh). If Telegram accepts a message but the process crashes before the state is persisted, a duplicate remains possible.
* A fresh run with empty/lost state, a skipped schedule, or a retry can **never** flood you with history: signals older than the freshness window, or already resolved by TP/SL, are skipped.
* Workflow `concurrency` serialises runs, so two overlapping runs cannot double-send.
* Residual risk: if Telegram accepts a message but the final push of the state branch fails, the same signal may be re-sent once on the next run.

## GitHub Actions scheduling limitations
* Scheduled runs are **best-effort**. GitHub can delay them (often several minutes, more at busy times such as the top of the hour) and may occasionally drop them. The freshness window (default 420 min for 1h) and ID registry are designed for this, but a delay longer than the window means that signal is skipped (tune with `MAX_SIGNAL_AGE_MINUTES_15M` / `_1H`).
* This project scans hourly at minute 5 UTC so the prior 1h candle has time to close. GitHub scheduled runs can still be delayed or skipped.
* `schedule` runs only on the **default branch**.
* In **public** repositories GitHub disables scheduled workflows after 60 days without repository activity; re-enable them in the Actions tab. (Check GitHub's current docs for whether bot commits count as activity.)
* **Public vs private:** public repos get free Actions minutes. A private repo on a free plan has a monthly minute quota; 96 runs/day ≈ 2,900 runs/month, each billed at least 1 minute, which can exceed a 2,000-minute quota. Signals and the state branch are not secret, secrets are never printed, but logs of a public repo are public.
* GitHub API / Telegram / MEXC outages simply cause that run to fail or skip; the next run catches up within the freshness window.

## Configuration (environment variables, all optional)
`TOP_N_SYMBOLS` (150), `CANDLE_LIMIT` (500, max 1000), `CANDLE_CLOSE_BUFFER_SECONDS` (5),
`MAX_SIGNAL_AGE_MINUTES_15M` (30, applies only if manually overriding timeframe), `MAX_SIGNAL_AGE_MINUTES_1H` (420), `STATE_RETENTION_DAYS` (14), `STATE_PATH`.

Symbol selection: MEXC Futures `contract/detail` (USDT quote + settlement, perpetual contracts only) joined with `contract/ticker`
(`amount24` turnover, descending); stablecoin bases (USDC, FDUSD, DAI, …) and leveraged-token-like bases (`…3L/3S/5L/5S`) are excluded.

## Known limitations
* **Not tested against the live MEXC or Telegram APIs from the build environment** (network access was restricted). The HTTP layer is tested against a local fake server that mimics the documented response shapes; the first real run (use `dry_run`) is the true integration test. MEXC Futures uses `Min15` and `Min60` intervals, and column-oriented candle arrays with Unix-second timestamps.
* Equivalence with TradingView is unverified (see differences above).
* 500-candle window and per-run recomputation (see difference 1).
* No TP/SL-hit notifications, no position tracking, no order execution — by design.
