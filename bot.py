"""NQ trading bot: runs the strategy live on today's session, in paper (simulated) mode.

    python bot.py

Every minute it pulls the latest 1-minute bars, replays today's session through the strategy, and
writes the result to bot_state.json (current position, trades, P&L) and journal.json (every
day's trades). Because it replays from the open each time, restarting mid-session is safe.

Paper mode only: no broker is connected and no real orders are sent.
"""
from __future__ import annotations

import json
import time as systime
from datetime import datetime
from pathlib import Path

import yfinance as yf

from backtest import bars_from_history
from market_data import ET, TICKER, get_pivots
from strategy import Config, SessionTrader

STATE_FILE = Path("bot_state.json")
JOURNAL_FILE = Path("journal.json")
POLL_SECONDS = 60


def run_once(cfg: Config) -> dict:
    now = datetime.now(ET)
    trader = SessionTrader(get_pivots(), cfg)
    data = yf.Ticker(TICKER).history(period="1d", interval="1m")
    if not data.empty:
        current_minute = now.replace(second=0, microsecond=0)
        for bar in bars_from_history(data):
            # only completed bars from today's session; the still-forming bar is skipped
            if bar.ts.date() == now.date() and bar.ts < current_minute:
                trader.on_bar(bar)

    status = {"mode": "paper", "symbol": cfg.symbol, "updated_at": now.isoformat(), **trader.status()}
    STATE_FILE.write_text(json.dumps(status, indent=2))

    journal = json.loads(JOURNAL_FILE.read_text()) if JOURNAL_FILE.exists() else {}
    if trader.trades:
        journal[now.date().isoformat()] = status["trades"]
        JOURNAL_FILE.write_text(json.dumps(journal, indent=2))
    return status


def main() -> None:
    cfg = Config()
    print(f"Paper-trading {cfg.contracts} {cfg.symbol}, max loss ${cfg.max_daily_loss:.0f}/day. Ctrl+C to stop.")
    seen = 0
    while True:
        try:
            status = run_once(cfg)
            if len(status["log"]) < seen:  # new session started
                seen = 0
            for line in status["log"][seen:]:
                print(f"{datetime.now(ET):%H:%M:%S}  {line}")
            seen = len(status["log"])
        except Exception as exc:  # keep running through data hiccups
            print(f"{datetime.now(ET):%H:%M:%S}  error: {exc}")
        systime.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
