# Architecture and implementation plan

This document covers the assessment for autonomous NQ trading (Phase 1), the chosen design
(Phase 2), and the status of every phase. Facts about third-party products were checked in
October 2026. Re-verify prices and eligibility before you rely on them.

## 1. Feasibility: what TradingView can and can't do

| Question | Answer | Consequence |
|---|---|---|
| Can TradingView run an autonomous agent? | No. Pine Script runs on TradingView's servers, but only as indicators and strategies on chart data. It can't hold state across restarts, call APIs, reconcile positions or manage risk. | The brain must be a separate always-on service. |
| Can TradingView alerts drive execution? | Technically, via webhooks (paid plans only). Webhooks are fire-and-forget HTTP POSTs from four fixed IPs, to ports 80/443 only, with a 3-second timeout, and TradingView documents that deliveries can fail. You get no acknowledgment, no position feedback and no retries you control. | Alerts are unsuitable as the *source of truth* for orders. This system uses them only for notifications. |
| Can alerts carry credentials safely? | Alert messages are stored at TradingView, and the webhook can't send auth headers. | Broker credentials never go near TradingView. The optional webhook uses a separate, low-privilege secret that can only post notes. |
| Is TradingView still useful? | Yes, for charting, for visual confirmation of the bot's levels, and because brokers such as Tradovate connect natively, so you can watch the bot's orders on your TradingView chart. | Ship a Pine indicator that mirrors the engine, plus an optional notification webhook. |

**Decision:** signals are computed by the engine from broker market data. TradingView is the
visual layer.

## 2. Broker options for NQ/MNQ futures

| | Tradovate (recommended) | Interactive Brokers | Rithmic (via prop/FCM) |
|---|---|---|---|
| API | REST + WebSocket; has a demo environment | TWS API via IB Gateway | R\|API (C++/.NET), Protocol Buffer API |
| TradingView link | Native broker integration | Native broker integration | Via some FCMs |
| Access requirements | Live account with **> $1,000** balance, plus the **API Access add-on (~$25/mo)** per Tradovate's forum. Prop/evaluation accounts reportedly excluded. | Funded account; Gateway needs periodic re-auth (2FA) and a daily restart | Conformance testing before production access |
| Real-time data via API | Separate CME data fees. Third-party sources cite $290–500/mo for API data; **unverified**, confirm with Tradovate. | Low-cost CME non-pro subscriptions | Through the FCM |
| Fit | Simplest modern API, bracket (OSO) orders, futures-native | Cheapest and most robust long-term, but clunkier operationally | Lowest latency; overkill for 1-minute strategies |

**Decision:** build against a broker *interface* (`nqtrader/brokers/base.py`). Implement Tradovate
first, with Interactive Brokers as the fallback adapter. The engine is broker-agnostic.

## 3. Data

* **Research:** 1-minute OHLCV for NQ, ideally 4+ years so walk-forward has 12+ folds and
  covers several regimes. Sources: **Databento** (CME Globex MDP 3.0; 1-minute OHLCV available;
  usage-based pricing with $125 sign-up credit, which should cover years of 1-minute bars; check
  their cost estimator) or **FirstRate Data** (continuous NQ 1-minute data from 2008).
  `nqtrader/data.py` reads Databento, TradingView-export and generic CSVs.
* **Paper trading:** Yahoo `NQ=F` (free, ~10 min delayed) via `feeds.YahooPollFeed`, for paper only.
* **Live:** the broker's real-time feed (the Tradovate WebSocket `md/subscribeChart`), in Phase 4.
* **Continuous contracts:** strategies are intraday and flat by 15:55, so roll gaps fall
  overnight. Prior-day levels can still straddle a roll; use back-adjusted data or skip roll
  days if a strategy depends on them.

## 4. System design

```
          TradingView (charts, Pine overlay, optional alert → webhook = notification only)
                                         │
 ┌──────────────── VPS / always-on host ─┼──────────────────────────────────────────────┐
 │                                       ▼                                              │
 │  Market data ──► ENGINE (nqtrader.engine) ──► RISK MANAGER (risk.py) ──► BROKER       │
 │  feed             │  strategy (production    │ hard limits, lockouts,    adapter      │
 │  (feeds.py)       │  version only)           │ sizing, health gate       (paper /     │
 │                   │  scanner, alerts,        │                            Tradovate)  │
 │                   │  exit plan, timers       │                               │        │
 │                   ▼                          ▼                               ▼        │
 │            SQLite (store.py, WAL): bars, orders, trades, events, equity, control      │
 │                   ▲                                                                   │
 │  DASHBOARD (server.py + dashboard/): read-only views; kill switch; price alerts       │
 │  RESEARCH (research/): walk-forward, baselines, gates → experiments → versions.py     │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

Separation of concerns, as you specified:

| Concern | Module | Can it place orders? |
|---|---|---|
| Strategy research & backtesting | `nqtrader/research/` | No |
| Signal generation | `research/strategies.py` (same code in backtest and live) | No |
| Risk management & budgets | `nqtrader/risk.py` | Vetoes only |
| Order execution & reconciliation | `nqtrader/engine.py`, `brokers/` | Yes (the only path) |
| Monitoring & controls | `nqtrader/server.py`, `dashboard/` | No (kill switch only) |
| News, calendar, earnings | `nqtrader/events.py` | No (blackouts feed risk) |
| Versioning & promotion | `nqtrader/versions.py` | No |

### Key design decisions

1. **One engine for backtest and live.** The live engine runs the identical strategy objects and
   the same fill model (`fills.py`). `tests/test_engine.py::test_engine_matches_backtest`
   proves trade-for-trade parity on five strategies. It already caught two real
   discrepancies: tick rounding and flatten timing.
2. **Conservative fills.** Next-bar-open entries, 1 tick of slippage on market and stop fills,
   gaps fill at the open, targets need a one-tick trade-through, and a bar touching both the stop
   and the target counts as a stop.
3. **Unknown means unsafe.** No new entry unless the broker is connected, positions are
   reconciled, any open position has a confirmed resting stop, data is fresh, and the
   contract is confirmed (`risk.SystemHealth`).
4. **Recovery procedure** (`engine._reconcile`): if a stop is missing, re-place it, and flatten if
   that fails. If the broker shows a position the engine doesn't know, protect it and lock out.
   If the engine expects a position the broker doesn't have, adopt the broker's state. Every
   case is logged as critical and locks out new entries for the day.
5. **Idempotency.** Each order's client id is a hash of strategy, day, bar and side, stored
   with a unique key and treated as idempotent by the adapters. A flatten is sent once per
   position.
6. **Humans own risk.** Limits live in `config/risk.json`, validated by pydantic on load,
   digested and logged. No code path writes them. The dashboard can't change them, place
   orders, or enable live trading.
7. **Controlled improvement.** Research writes reproducible experiments (data hash, git sha,
   config). `promote` succeeds only for a strategy and parameter set that passed every gate in
   a named experiment. `rollback` is one command. Nothing self-modifies.

### Validation gates (research/walkforward.py)

A strategy becomes a paper-trading *candidate* only if, out of sample and after costs, all of
these hold: at least 100 trades, profit factor ≥ 1.15, Sharpe ≥ 0.75, the 95% bootstrap CI of
expectancy is above $0, at least 55% of walk-forward windows are profitable, the untouched
holdout is profitable, and it beats the random-entry baseline. Live trading additionally needs a
paper track record on real data (`preflight.py`).

The gates were calibrated on synthetic data. On a pure random walk every candidate is rejected,
and an injected 2% drift edge is detected while a 1% one isn't (see `tests/test_backtest.py`).

## 5. Hosting and costs (estimates)

| Item | Cost | Notes |
|---|---|---|
| VPS (2 vCPU, 4 GB, US East/Chicago) | $10–40/mo | systemd units in `deploy/` |
| Tradovate API add-on | ~$25/mo | plus live account > $1,000 |
| Real-time CME data via broker API | varies; possibly $$$ | confirm before Phase 4; IBKR is cheaper |
| Historical data (Databento) | likely within $125 free credit | 1-minute OHLCV is small |
| TradingView | paid plan only if you want webhooks | charts work on any plan |

## 6. Phase status

| Phase | Status | What exists |
|---|---|---|
| 1. Feasibility | **Done** | This document |
| 2. Architecture | **Done** | Python 3.11+, FastAPI, SQLite (WAL), vanilla JS + TradingView Lightweight Charts, systemd |
| 3. Research & backtesting | **Done (needs real data)** | 5 candidates + 3 baselines, walk-forward, holdout, regimes, sensitivity, gates, reports, experiment registry |
| 4. Risk, broker, execution, recovery | **Risk/execution/recovery done; paper broker done; Tradovate adapter NOT built** | Needs your Tradovate demo credentials to build and test against |
| 5. Dashboard | **Done** | 8 sections, verified in Chromium at desktop and phone widths |
| 6. TradingView | **Done (untested on TradingView)** | Pine v6 overlay + notification webhook; compile it in Pine Editor |
| 7. Paper trading & stress tests | **Started** | Synthetic replay + fault-injection tests pass; live paper run on Yahoo data not yet done (Yahoo is blocked in the build sandbox) |
| 8. Deployment & launch checklist | **Done (docs)** | `docs/OPERATIONS.md`, `deploy/`, `python -m nqtrader authorize-live` |

## 7. What's needed from you

1. **Historical data:** a CSV of NQ 1-minute bars, 2021 to now (Databento `GLBX.MDP3`,
   `ohlcv-1m`, `NQ.v.0` or `NQ.c.0`). Then run `python -m nqtrader research --data that.csv`.
   That run gives the first real answer to "is there an edge?"
2. **Your economic calendar:** the full FOMC/CPI/NFP/PPI schedule in
   `config/economic_calendar.csv`. Only the dates I could verify are there now.
3. **Broker decision and demo credentials** (Tradovate demo is free), so I can build and test
   the live adapter against their simulator before any real money is involved.
4. **Your risk budget.** The default $150/trade on $25k can't fit a 10:00-range stop on even one
   MNQ contract most days: the engine will (correctly) block those trades. Decide the capital
   and per-trade risk you actually accept.
