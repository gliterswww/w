"""Wires the engine to a store, broker, data feed and configuration, and runs it."""
from __future__ import annotations

import time as systime
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import events as ev
from . import versions
from .brokers import PaperBroker
from .data import ET, split_sessions
from .engine import Engine
from .feeds import ReplayFeed, YahooPollFeed
from .fills import CostModel
from .instruments import INSTRUMENTS
from .risk import load_limits
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def build_engine(store: Store, *, history: list, data_label: str, limits_path: Path,
                 instrument: str = "MNQ", mode: str = "paper") -> Engine:
    if mode != "paper":
        raise SystemExit("Only paper mode is available: no live broker adapter is configured yet. "
                         "See docs/ARCHITECTURE.md (Phase 4).")
    inst = INSTRUMENTS[instrument]
    limits = load_limits(limits_path)
    cost = CostModel(inst)
    v = versions.production(store)
    broker = PaperBroker(cost, starting_balance=limits.account_capital + _realized(store))
    earnings = ev.load_earnings_csv(CONFIG / "earnings.csv") + (store.get("earnings_cache") or [])
    store.set("mode", mode)
    store.set("data_label", data_label)
    return Engine(store=store, broker=broker, instrument=inst, limits=limits, strategy_name=v["strategy"],
                  strategy_params=v["params"], strategy_version=v["version"], history=history,
                  calendar=ev.load_calendar(CONFIG / "economic_calendar.csv"), earnings=earnings,
                  mode=mode, cost=cost, data_label=data_label)


def _realized(store: Store) -> float:
    rows = store.query("SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades WHERE mode='paper'")
    return float(rows[0]["s"])


def run_replay(df: pd.DataFrame, store: Store, *, trade_sessions: int, data_label: str,
               limits_path: Path, instrument: str = "MNQ") -> Engine:
    sessions = split_sessions(df)
    if len(sessions) <= trade_sessions + 15:
        raise ValueError("not enough sessions to replay")
    history = sessions[:-trade_sessions]
    start_day = sessions[-trade_sessions].day
    eng = build_engine(store, history=history, data_label=data_label, limits_path=limits_path, instrument=instrument)
    feed = ReplayFeed(df[df.index.date >= start_day], label=data_label)
    last_day = None
    for bar in feed:
        if last_day and bar.ts.date() != last_day:
            eng.scan()
        eng.on_bar(bar)
        last_day = bar.ts.date()
    eng.scan()
    return eng


def run_live_paper(store: Store, *, limits_path: Path, instrument: str = "MNQ", poll_seconds: int = 20) -> None:
    feed = YahooPollFeed()
    try:
        hist_df = feed.history()
    except Exception as exc:
        raise SystemExit(f"Could not load history from Yahoo Finance: {exc}")
    today = datetime.now(ET).date()
    history = [s for s in split_sessions(hist_df) if s.day < today]
    eng = build_engine(store, history=history, data_label=feed.label, limits_path=limits_path, instrument=instrument)
    store.log("warning", "data", "Paper trading on Yahoo data, which is ~10 min delayed. Fills are approximate.")
    print("Paper trading on delayed Yahoo data. Dashboard: python -m nqtrader serve. Ctrl+C to stop.")
    while True:
        now = datetime.now(ET)
        store.set("engine_heartbeat", {"at": now.isoformat(timespec="seconds"), "feed": feed.label,
                                       "feed_error": feed.error})
        for bar in feed.poll(now):
            eng.on_bar(bar, now=now)
            eng.scan()
        if feed.error:
            store.log("error", "data", f"Data feed error: {feed.error}")
        systime.sleep(poll_seconds)


def refresh_external(store: Store) -> dict:
    """Pull news and earnings dates (network). Cached in the store for the dashboard."""
    news, news_err = ev.fetch_news()
    earn, earn_err = ev.fetch_earnings_yahoo()
    now = datetime.now(ET).isoformat(timespec="seconds")
    store.set("news_cache", {"items": news, "error": news_err, "fetched": now})
    if earn:
        store.set("earnings_cache", earn)
    store.set("earnings_fetch", {"error": earn_err, "fetched": now, "count": len(earn)})
    return {"news": len(news), "news_error": news_err, "earnings": len(earn), "earnings_error": earn_err}
