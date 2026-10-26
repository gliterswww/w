"""Market data feeds for the engine.

* ReplayFeed: historical or synthetic bars, played back in order (demo, stress tests).
* YahooPollFeed: Yahoo Finance's free NQ=F 1-minute bars. They run about 10 minutes behind,
  so this feed is for paper trading only. Live trading must use the broker's real-time feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .data import ET, RTH_CLOSE, RTH_OPEN
from .engine import Bar


def in_rth(ts: datetime) -> bool:
    t = ts.time()
    return RTH_OPEN <= t < RTH_CLOSE and ts.weekday() < 5


def bars_from_frame(df: pd.DataFrame) -> list[Bar]:
    return [Bar(ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
            for ts, r in zip(df.index, df.itertuples(index=False))]


class ReplayFeed:
    label = "replay"

    def __init__(self, df: pd.DataFrame, label: str = "replay"):
        self.bars = [b for b in bars_from_frame(df) if in_rth(b.ts)]
        self.label = label

    def __iter__(self):
        return iter(self.bars)


class YahooPollFeed:
    label = "yahoo-delayed"
    ticker = "NQ=F"

    def __init__(self):
        self.last_ts: datetime | None = None
        self.error: str | None = None

    def history(self, days: int = 30) -> pd.DataFrame:
        import yfinance as yf
        df = yf.Ticker(self.ticker).history(period=f"{min(days, 7)}d", interval="1m")
        return _normalize(df)

    def poll(self, now: datetime) -> list[Bar]:
        """New *completed* RTH bars since the last poll."""
        import yfinance as yf
        try:
            df = _normalize(yf.Ticker(self.ticker).history(period="1d", interval="1m"))
            self.error = None if not df.empty else "no data returned"
        except Exception as exc:  # network, rate limit, schema change
            self.error = str(exc)
            return []
        cutoff = now.replace(second=0, microsecond=0)
        out = []
        for b in bars_from_frame(df):
            if b.ts + timedelta(minutes=1) > cutoff or not in_rth(b.ts):
                continue
            if self.last_ts is None or b.ts > self.last_ts:
                out.append(b)
        if out:
            self.last_ts = out[-1].ts
        return out


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    df.index = df.index.tz_convert(ET)
    return df
