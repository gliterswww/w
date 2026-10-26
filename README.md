# NQ Autotrader

A research-first, risk-managed automated trading system for Nasdaq-100 futures (NQ / MNQ),
with a web dashboard and TradingView integration.

> **Status: paper trading only.** No live broker is connected, and no real orders can be sent.
> Results shown so far come from **synthetic** data and say nothing about real markets. The
> system won't claim an edge it hasn't measured, and so far it hasn't been run on real NQ history.

## What's in it

| Component | What it does |
|---|---|
| **Strategy research** | Walk-forward tests 5 NQ strategies (10:00 opening-range breakout, VWAP pullback, VWAP reversion, liquidity-sweep failed breakout, late-day momentum) against no-trade, random-entry and buy-the-open baselines, after commissions and slippage. Includes a holdout period, regimes, parameter sensitivity and statistical gates. |
| **Chart scanner** | Replays every strategy over today's bars live: which are watching, signalling, or in a trade, plus key levels (VWAP bands, 10:00 range, prior day). |
| **Risk manager** | Hard limits that override every signal: risk per trade, daily/weekly/monthly loss, drawdown, consecutive losses, trades/day, max contracts, slippage, profit lock, session window, news blackout. It also blocks entries whenever broker, positions, stops, data or contract are unconfirmed. |
| **Budget manager** | Capital, equity high-water mark, and remaining daily, weekly and monthly loss budgets. |
| **Order executor** | Bracket orders (entry + stop + target), idempotent client ids, reconciliation with the broker every bar, automatic recovery, one-shot flatten, emergency stop. |
| **Exit planner** | For each position: stop, target, R ladder, $ at each level, time exit. |
| **Entry timer** | Today's schedule (range complete, entry windows, cut-offs, flatten, news events) with countdowns, plus research stats by entry hour. |
| **News monitor** | Topic-tagged headlines, plus an economic calendar whose high-impact events block entries. |
| **Earnings analyst** | Mega-cap Nasdaq-100 report dates mapped to the NQ session that reacts. Research splits performance on those days. |
| **Price alerts** | Set from the dashboard, checked every bar, delivered as a toast or browser notification. Alerts never trade. |
| **Versioning** | Strategies are promoted only with a passing experiment, and rollback is one command. Nothing rewrites itself. |
| **TradingView** | A Pine v6 overlay of the bot's levels and signals, plus an optional alert webhook (notifications only). |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                 # 58 tests

python -m nqtrader demo --reset     # synthetic research + 30-session paper replay
python -m nqtrader serve            # → http://127.0.0.1:8000
```

## With real data

```bash
# 1-minute NQ bars (Databento, TradingView export, or generic CSV), ideally 2021 → today
python -m nqtrader research --data nq_1m.csv --instrument NQ
# If, and only if, a strategy passes every gate:
python -m nqtrader promote orb --params '{"minutes": 30, "target_r": 2.0}' --experiment <id>
python -m nqtrader paper            # paper-trade today on delayed Yahoo data
python -m nqtrader refresh          # headlines + earnings dates
python -m nqtrader authorize-live   # pre-live checklist (live adapter not built yet)
```

Other commands: `versions`, `rollback`, `kill`, `resume`. See `python -m nqtrader -h`.

## Configuration

* `config/risk.json`: your risk limits (validated on load; the system never changes them).
  `risk.paper.json` relaxes data staleness for delayed Yahoo data; `risk.demo.json` is for the demo.
* `config/economic_calendar.csv`: scheduled releases (`datetime_et,event,impact,source`).
  **Fill in the full schedule.** Only verified FOMC and CPI dates are included.
* `config/earnings.csv`: optional manual earnings dates (`symbol,date,timing` with `bmo`/`amc`).
* Secrets go in environment variables only: see `deploy/env.example`.

## Docs

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): feasibility, broker/data choice, design,
  validation gates, costs, phase status, and what's needed next.
* [`docs/OPERATIONS.md`](docs/OPERATIONS.md): deployment, monitoring, backups, runbooks, and the
  live-launch checklist.
* `tradingview/nq_autotrader_levels.pine`: paste into the TradingView Pine Editor.

`legacy/` holds the original read-only session dashboard.

**Not financial advice.** Futures trading involves substantial risk of loss. Backtests and paper
results are simulations, not evidence of future performance.
