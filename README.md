# Kalshi research bot

A **paper-only** BTC, ETH, and SOL 15-minute market research bot. It collects Kalshi market data, evaluates the frozen baseline, simulates delayed fills against executable depth, and keeps a shared $250 SQLite account. There is no live order submission client.

## Run the demo

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/MGM-EE/Kalshi_Bot-V1.git
cd Kalshi_Bot-V1
uv sync --frozen --extra dev
uv run --no-sync kalshi-bot demo
uv run --no-sync kalshi-bot dashboard
```

Open `http://127.0.0.1:8000` in a browser on the same machine. The demo completes a synthetic round quickly; the dashboard then reads the saved results. It displays a fixed PAPER banner and identifies synthetic data. **Demo P&L is fabricated market data for functional testing and says nothing about strategy profitability.** Repeating the same demo directory resumes safely without duplicate entries.

In this prepared cloud checkout, dependencies are already installed. Run `.venv/bin/kalshi-bot demo`, followed by `.venv/bin/kalshi-bot dashboard`. A cloud terminal's loopback address is local to that machine; no public preview or hosting is configured.

If uv is unavailable, create a Python virtual environment and run `python -m pip install -e '.[dev]'`; this installs compatible versions rather than reproducing the exact `uv.lock` environment.

## Windows desktop shortcut

In the extracted bot folder, double-click **Create Desktop Shortcut.cmd** once. It creates **Kalshi Bot (Demo)** on your Windows desktop, including desktops stored in OneDrive. No administrator account is needed.

Double-click that desktop icon to start the bot and open its dashboard in your browser. On first launch, it prepares the virtual environment/dependencies if needed and generates the demo; this can take a minute or more on Windows. Later launches reuse the prepared demo. Keep the launcher window open while using the bot. Close that window or press Ctrl+C in it to stop; closing the browser tab alone leaves the launcher running. A second click on the icon opens the existing instance.

You can also double-click **Launch Bot.cmd** directly, or run `kalshi-bot desktop` on any supported platform. The launcher chooses another local port if 8000 is in use. It preserves existing data and keeps each code/configuration version in its own `data/desktop/` directory. Keep the bot folder in place after creating the shortcut; recreate the shortcut if you move it. To use an updated ZIP, extract it into a new folder and run its shortcut installer; preserve your old folder and data. If the installer finds an existing shortcut pointing elsewhere, rename that shortcut before installing the new one.

For a separate real-data **paper** shortcut after configuring credentials and health/fee settings, run this once from the bot folder in PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\Create Desktop Shortcut.ps1" -Mode paper
```

This creates **Kalshi Bot (Paper)**, which starts the real-data collector and dashboard together. Without the required feed access and settings, it collects data with entries paused.

## Live chart and trade markers

The enlarged market chart updates every second while the real-data collector is running. Choose BTC, ETH or SOL, a market round, and a time range. Switch between **UP / DOWN prices** (separate observed bids and dashed asks) and **Crypto / strike** (CF reference and official strike). Hover for received prices/times; click a trade marker for its quantity, actual paper fill price, and fee.

Triangles mark **BUY UP / BUY DOWN**; squares mark **SELL UP / SELL DOWN**, colored to match the selected outcome. The chart uses the actual recorded outcome price, including NO fills; settlement payouts are listed separately and never presented as sales. Missing feeds, gaps, polling-only books, and stale data remain visible. The dashboard cannot place orders.

In demo mode, the chart automatically plays the recorded synthetic round at **30× speed** once preparation finishes. Use pause, replay, speed, and scrubber controls to inspect trades. BTC demonstrates an UP buy/sell and ETH a DOWN buy/sell; SOL demonstrates holding through settlement. Playback changes the chart's observation time only; portfolio cards show the completed demo totals. It creates no new trades or performance evidence.

## Collect real data

```bash
uv run --no-sync kalshi-bot paper --config config.example.toml
```

This starts the collector in the foreground and writes `data/paper/paper.sqlite3` plus compressed raw-event mirrors. Without credentials, public metadata, book snapshots, and official settlements are collected; **entries remain paused** because polling cannot establish stream continuity or provide the baseline CF reference feed.

For authenticated read-only WebSocket feeds, set these in your process environment:

```bash
export KALSHI_API_KEY_ID='your-key-id'
export KALSHI_PRIVATE_KEY_PATH='/absolute/path/to/your-private-key.pem'
uv run --no-sync kalshi-bot paper --config config.example.toml
```

Keep the PEM outside Git and restrict its file permissions. The adapter signs WebSocket GET authentication locally using RSA-PSS; a proxy credential placeholder is not a PEM key. Keys are never written to the journal or exports. A read-only Kalshi API credential is sufficient; the program has no order placement routes. Actual feed entitlements and authenticated production connectivity need verification with your account.

The default remains collect-only until the reference feed, complete lookback, book continuity, exact rules/times/ticks, fee configuration, and host clock uncertainty are usable. `clock_uncertainty_ms` must be supplied from a host measurement (maximum 100 ms). An optional explicitly named `fee_scenario` enables **unverified scenario simulation**; it is never labeled fee-exact. Actual series/event fee metadata is retained independently. Do not enter guessed values to make health gates green.

ETH/SOL thresholds freeze from asset and BTC references at market opening. Starting after an opening can therefore pause these assets until the next round. Missing references never cause automatic substitution with Coinbase or Kraken prices.

Run the read-only dashboard in a second terminal:

```bash
uv run --no-sync kalshi-bot dashboard --data-dir data/paper
```

Use `--duration 60` for a bounded collector run and Ctrl-C for a clean shutdown. Keep using the same data directory to resume the same code/configuration. A changed configuration or code hash requires a new directory, preserving the old run for comparison. After a real collector restart, pending orders are canceled and a fresh verified book and lookback are required; the bot never fills orders retroactively through downtime.

Continuous collection needs a user-controlled machine that stays on. This repository, an open chat, and a GitHub pull request do not deploy an always-on service. The example systemd units under `deploy/` can be adapted for a Linux host.

## What the baseline does

- Evaluate at one-second receipt-time boundaries with 31 causal samples and a two-second reference-age limit.
- Enter the currently losing side with 120–600 seconds left, distance at most 0.2%, sufficient approach momentum, projected reach before cutoff, spread at most six cents, and all-in cost at most 40 cents.
- Use exact Decimal fees, complementary YES/NO books, current price ranges, conservative 50% visible depth, and a 500 ms paper arrival delay. Revalidate signal, budget, fees, spread, and depth at arrival; partial-fill prefixes must respect the cost ceiling.
- Reserve shared cash transactionally; cap each entry at the lesser of $5 and 2% of available cash. Permit one filled entry per asset-market round, without top-ups.
- Attempt an executable sale at 95 cents or better; otherwise wait for observed official settlement. Partial exits retain the remaining position. Cutoff alone never releases settlement cash.
- Pause new entries after $25 daily realized loss or four consecutive completed losing positions. Stops reset at New York midnight; existing positions retain their exit policy.

The detailed supplied design is retained in [docs/SPECIFICATION.md](docs/SPECIFICATION.md). Trend diagnostics are logged without silently tightening the baseline. Raw exchange research streams do not affect the baseline signal.

## Replay, research, and exports

```bash
# Recreate a recorded run in a NEW directory. SQLite includes its source configuration.
uv run --no-sync kalshi-bot replay data/demo/paper.sqlite3 --data-dir data/demo-replay

# JSON reports, paired exit comparisons, and lossless monetary-string Parquet exports.
uv run --no-sync kalshi-bot report --data-dir data/demo
uv run --no-sync kalshi-bot compare-exits --data-dir data/demo
uv run --no-sync kalshi-bot export --data-dir data/demo --output data/exports

# Engineering tests.
uv run --no-sync pytest -q
```

Gzip/JSONL replay is also supported. Keep `run.json` with copied raw data, or provide the original TOML via `--config`. SQLite's committed event journal is authoritative; the gzip files are an audit mirror and an interrupted append does not alter account transactions.

The dashboard provides filtered, paginated trade history, CSV/JSON downloads, feed freshness, reference/strike and contract quote charts, actual fill markers, portfolio exposure, trajectory distributions, and paired exit tables. Unknown prices stay visibly missing; unavailable remaining depth receives an explicit zero valuation flag, never an invented sale.

Research tools implement MFE/MAE, horizon coverage, executable-level touches, fees/net returns, paired fixed/trailing/time/reversal exits with settlement residuals, immutable chronological split manifests, causal future labels, training-only scaling, unusual-flow diagnostics, and seeded day-block bootstrap helpers. Exit comparisons use the first observed one-second trajectory snapshot after the configured delay; this timing assumption is reported separately from the baseline engine's receipt-time timers. Full-portfolio capital-reuse simulation, a calibrated predictive model, validated exchange book reconstruction/latency selection, and the multiweek forward study remain future research. Missing experimental flow features cause an explicit baseline fallback. No probability or AI confidence is fabricated.

## Layout

| Path | Purpose |
|---|---|
| `kalshi_bot/feeds.py` | GET-only discovery, authenticated reference/book streams, sequence recovery, official settlements, optional raw exchange feeds |
| `kalshi_bot/strategy.py` | Baseline gates, exact fees, complementary books, depth-aware quotes |
| `kalshi_bot/ledger.py` | Atomic shared account, reservations, partial exits, risk stops |
| `kalshi_bot/engine.py` | Receipt-ordered decisions, paper arrival timers, journaling, restart recovery |
| `kalshi_bot/dashboard.py`, `static/` | Read-only local dashboard and downloads |
| `kalshi_bot/research.py` | Trajectories, comparisons, splits, labels, and Parquet |
| `tests/` | Strategy, ledger, feeds, dashboard, research, replay and restart tests |

Optional runtime destinations: `external-api.kalshi.com`, `external-api-ws.kalshi.com`; when explicitly enabled, `advanced-trade-ws.coinbase.com` and `ws.kraken.com`. Package installation uses PyPI. Keep TLS verification enabled. Use documented API-host alternatives only; arbitrary credential destinations are rejected.
