"""Replays recent NQ history through the bot's strategy and reports how it would have done.

    python backtest.py                 # last 7 days of 1-minute bars (Yahoo's 1m limit)
    python backtest.py --interval 5m --days 59
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import pandas as pd
import yfinance as yf

from market_data import ET, TICKER, pivots_from_hlc
from strategy import Bar, Config, SessionTrader, Trade


def bars_from_history(df: pd.DataFrame) -> list[Bar]:
    df = df.copy()
    df.index = df.index.tz_convert(ET)
    return [
        Bar(ts=ts.to_pydatetime(), open=float(r["Open"]), high=float(r["High"]),
            low=float(r["Low"]), close=float(r["Close"]))
        for ts, r in df.iterrows()
    ]


def run_backtest(intraday: pd.DataFrame, daily: pd.DataFrame, cfg: Config) -> list[Trade]:
    by_day: dict = defaultdict(list)
    for bar in bars_from_history(intraday):
        by_day[bar.ts.date()].append(bar)

    daily_dates = [ts.date() for ts in daily.index]
    trades: list[Trade] = []
    for day in sorted(by_day):
        prior = [i for i, d in enumerate(daily_dates) if d < day]
        if not prior:
            continue
        row = daily.iloc[prior[-1]]
        trader = SessionTrader(pivots_from_hlc(float(row["High"]), float(row["Low"]), float(row["Close"])), cfg)
        for bar in by_day[day]:
            trader.on_bar(bar)
        trades.extend(trader.trades)
    return trades


def summarize(trades: list[Trade]) -> str:
    if not trades:
        return "No trades taken."
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    lines = [
        f"Trades:        {len(trades)}",
        f"Win rate:      {len(wins) / len(trades):.0%}",
        f"Total P&L:     ${sum(pnls):+,.2f}",
        f"Avg win:       ${(sum(wins) / len(wins)) if wins else 0:+,.2f}",
        f"Avg loss:      ${(sum(losses) / len(losses)) if losses else 0:+,.2f}",
        f"Max drawdown:  ${max_dd:,.2f}",
        "",
        "By setup:",
    ]
    by_setup: dict = defaultdict(list)
    for t in trades:
        by_setup[f"{t.label} {t.side}"].append(t.pnl)
    for name, ps in sorted(by_setup.items()):
        lines.append(f"  {name:<24} {len(ps):>3} trades  ${sum(ps):+,.2f}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", default="1m", help="bar size: 1m (max 7 days) or 5m (max 59 days)")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    ticker = yf.Ticker(TICKER)
    intraday = ticker.history(period=f"{args.days}d", interval=args.interval)
    daily = ticker.history(period="3mo", interval="1d")
    if intraday.empty or daily.empty:
        raise SystemExit("Couldn't download price data from Yahoo Finance.")

    trades = run_backtest(intraday, daily, Config())
    for t in trades:
        print(f"{t.opened_at[:16]}  {t.label:<17} {t.side:<5} {t.entry:>9.2f} -> {t.exit:>9.2f}  "
              f"{t.reason:<10} ${t.pnl:+8.2f}")
    print()
    print(summarize(trades))


if __name__ == "__main__":
    main()
