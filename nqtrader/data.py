"""Loading 1-minute NQ bars and splitting them into regular trading sessions.

Accepted CSV layouts (auto-detected):
  * Databento ohlcv-1m:   ts_event, open, high, low, close, volume  (fixed-point prices are rescaled)
  * TradingView export:   time (unix seconds), open, high, low, close, Volume
  * Generic:              timestamp|datetime|date, open, high, low, close, volume

Timestamps are the bar open. Naive timestamps are assumed to be UTC.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
MIN_SESSION_BARS = 200  # a normal session has 390; early closes (13:15 ET) have 225

_TS_COLUMNS = ("ts_event", "timestamp", "datetime", "time", "date")


def load_bars_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    ts_col = next((c for c in _TS_COLUMNS if c in df.columns), None)
    if ts_col is None:
        raise ValueError(f"{path}: no timestamp column (expected one of {_TS_COLUMNS})")
    missing = {"open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")

    raw = df[ts_col]
    if np.issubdtype(raw.dtype, np.number):
        unit = "ns" if raw.iloc[0] > 1e14 else "s"
        ts = pd.to_datetime(raw, unit=unit, utc=True)
    else:
        ts = pd.to_datetime(raw, utc=True)

    out = pd.DataFrame({c: df[c].astype(float) for c in ("open", "high", "low", "close")})
    if out["close"].median() > 1e7:  # Databento fixed-point (1e-9) prices
        out = out / 1e9
    out["volume"] = df["volume"].astype(float) if "volume" in df.columns else 0.0
    out.index = ts.dt.tz_convert(ET)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    bad = (out["high"] < out[["open", "close"]].max(axis=1)) | (out["low"] > out[["open", "close"]].min(axis=1))
    return out[~bad]


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def frame_sha256(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()


@dataclass
class Session:
    """One regular trading session (9:30-16:00 ET) as numpy arrays, plus causal prior-day context."""
    day: date
    ts: np.ndarray        # datetime64[ns] of bar open (ET wall-clock, tz stripped)
    minute: np.ndarray    # minutes since midnight ET, int
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    # Known before the session opens (computed only from earlier sessions):
    prev_high: float = np.nan
    prev_low: float = np.nan
    prev_close: float = np.nan
    atr: float = np.nan   # mean RTH range of the previous 14 sessions
    extra: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.close)

    @property
    def range(self) -> float:
        return float(self.high.max() - self.low.min())


def split_sessions(bars: pd.DataFrame, atr_window: int = 14) -> list[Session]:
    idx = bars.index
    t = idx.hour * 60 + idx.minute
    rth = bars[(t >= RTH_OPEN.hour * 60 + RTH_OPEN.minute) & (t < RTH_CLOSE.hour * 60)]
    sessions: list[Session] = []
    for day, g in rth.groupby(rth.index.date):
        if len(g) < MIN_SESSION_BARS:
            continue
        sessions.append(Session(
            day=day,
            ts=g.index.tz_localize(None).values,
            minute=(g.index.hour * 60 + g.index.minute).to_numpy(),
            open=g["open"].to_numpy(), high=g["high"].to_numpy(),
            low=g["low"].to_numpy(), close=g["close"].to_numpy(),
            volume=g["volume"].to_numpy(),
        ))
    ranges: list[float] = []
    for i, s in enumerate(sessions):
        if i > 0:
            p = sessions[i - 1]
            s.prev_high, s.prev_low, s.prev_close = float(p.high.max()), float(p.low.min()), float(p.close[-1])
        if len(ranges) >= 5:
            s.atr = float(np.mean(ranges[-atr_window:]))
        ranges.append(s.range)
    return sessions


def session_vwap(s: Session) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative session VWAP and volume-weighted std of typical price around it.

    Both are causal: element i uses only bars 0..i.
    """
    tp = (s.high + s.low + s.close) / 3
    vol = np.where(s.volume > 0, s.volume, 1.0)
    cv = np.cumsum(vol)
    vwap = np.cumsum(tp * vol) / cv
    var = np.maximum(np.cumsum(tp * tp * vol) / cv - vwap * vwap, 0.0)
    return vwap, np.sqrt(var)


def to_et_datetime(ts64: np.datetime64) -> datetime:
    return pd.Timestamp(ts64).tz_localize(ET).to_pydatetime()
