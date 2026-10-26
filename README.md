# NQ Session Dashboard

A simple, clean dashboard for trading NQ1! (Nasdaq-100 futures) on TradingView. It shows:

- **News** — relevant headlines (Fed/rates, inflation, Nasdaq big-tech, earnings) pulled from free public feeds.
- **Entry Points** — up to 4 mechanically-derived levels for the session, combining two standard methods:
  - **Opening Range Breakout (ORB)**: long above / short below the first 15 minutes of the regular session (9:30–9:45 AM ET).
  - **Pivot Points**: classic floor-trader pivots (S1/S2, R1/R2) from the prior session's high/low/close.
- **Session Levels** — the raw ORB and pivot levels, for context.

The dashboard itself is read-only. There's also a separate **trading bot** (see below) that trades these same entries on its own, in paper (simulated) mode.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open **http://localhost:5000**. The page auto-refreshes every 60 seconds.

## How the entries are calculated

| Entry | Side | Trigger | Stop |
|---|---|---|---|
| ORB Breakout | Long | Opening range high + 5 pts | Opening range low |
| ORB Breakout | Short | Opening range low − 5 pts | Opening range high |
| Pivot Support | Long | S1 | S2 |
| Pivot Resistance | Short | R1 | R2 |

ORB entries only appear once the opening range has formed (9:45 AM ET); before that, pivot entries are shown on their own. All levels come straight from price data — there's no discretionary judgment involved, so treat them as a starting point, not a signal.

## Trading bot (paper mode)

The bot follows strict, fixed rules. It doesn't guess or "think": it trades the dashboard's entry points and applies risk limits on every trade.

```bash
python backtest.py                       # how would it have done over the last 7 days?
python backtest.py --interval 5m --days 59
python bot.py                            # run it live on today's session (simulated fills)
```

**What it does:**

- **ORB breakouts** go in as stop orders through the opening-range high/low. **Pivot reactions** go in as limit orders at S1/R1.
- A setup only goes live once price is on the right side of it. If price is already past the level, the bot treats it as missed and doesn't chase it.
- Every trade gets a stop (the entry's stop) and a take-profit at **2× the risk**.
- It holds one position at a time, and each setup trades at most once per day.

**Safety limits** (in `Config` in `strategy.py`):

| Setting | Default |
|---|---|
| Instrument | 1 × MNQ (micro, $2/point) |
| Skip setups with a stop wider than | 60 points |
| Max trades per day | 3 |
| Daily loss limit | $250 (no new trades after it's hit) |
| No new entries after | 3:00 PM ET |
| Flatten any open position at | 3:55 PM ET |

`bot.py` writes its current position, trades and P&L to `bot_state.json`, and each day's trades to `journal.json`. The backtest and the live bot share one engine (`SessionTrader`), so whatever you see in a backtest is exactly what the bot does live.

**Limitations:**

- **There's no broker connection.** All fills are simulated, so no real money is at risk. Live trading would need a futures broker API (Tradovate, Interactive Brokers, NinjaTrader, etc.) behind the same engine.
- **The data is delayed.** Yahoo's futures data runs about 10 minutes behind and has gaps, so paper results are only approximate.
- **Fills are simulated conservatively.** If a single bar touches both the stop and the target, the bot counts it as a stop. Slippage beyond gaps isn't modeled.
- **Yahoo only keeps a little history:** 7 days of 1-minute bars, or about 60 days of 5-minute bars. That's not much to judge a strategy on.

## Notes

- Market data comes from Yahoo Finance (`NQ=F`) via `yfinance`, no API key required.
- News comes from free RSS feeds (Yahoo Finance, MarketWatch), filtered by keyword relevance.
- If either data source is unreachable, the dashboard degrades gracefully and shows a "couldn't load" message instead of crashing.
- Everything runs locally: there are no accounts and no broker connection, and no real orders are ever sent.

**Not financial advice.** For informational purposes only — always manage your own risk.
