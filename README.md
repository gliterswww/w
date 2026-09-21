# NQ Session Dashboard

A simple, clean dashboard for trading NQ1! (Nasdaq-100 futures) on TradingView. It shows:

- **News** — relevant headlines (Fed/rates, inflation, Nasdaq big-tech, earnings) pulled from free public feeds.
- **Entry Points** — up to 4 mechanically-derived levels for the session, combining two standard methods:
  - **Opening Range Breakout (ORB)**: long above / short below the first 15 minutes of the regular session (9:30–9:45 AM ET).
  - **Pivot Points**: classic floor-trader pivots (S1/S2, R1/R2) from the prior session's high/low/close.
- **Session Levels** — the raw ORB and pivot levels, for context.

This is a read-only dashboard: it does not place trades. It just tells you the news and gives you levels to consider putting into TradingView yourself.

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

## Notes

- Market data comes from Yahoo Finance (`NQ=F`) via `yfinance`, no API key required.
- News comes from free RSS feeds (Yahoo Finance, MarketWatch), filtered by keyword relevance.
- If either data source is unreachable, the dashboard degrades gracefully and shows a "couldn't load" message instead of crashing.
- Everything runs locally — no accounts, no broker connection, no trades are ever placed automatically.

**Not financial advice.** For informational purposes only — always manage your own risk.
