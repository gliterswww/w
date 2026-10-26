"""Market data for NQ (Nasdaq-100 futures): session pivots and opening-range levels."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

TICKER = "NQ=F"
ET = ZoneInfo("America/New_York")

RTH_OPEN = (9, 30)   # regular trading hours open, ET
ORB_MINUTES = 15     # opening-range window used for breakout levels


@dataclass
class Pivots:
    pivot: float
    r1: float
    r2: float
    s1: float
    s2: float
    prior_high: float
    prior_low: float
    prior_close: float


@dataclass
class OpeningRange:
    high: float
    low: float
    formed: bool  # False while the session's opening range is still building/not started


def _now_et() -> datetime:
    return datetime.now(ET)


def get_current_price() -> float | None:
    try:
        data = yf.Ticker(TICKER).history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception:
        return None


def get_pivots() -> Pivots | None:
    """Classic floor-trader pivots from the prior completed session's H/L/C."""
    try:
        daily = yf.Ticker(TICKER).history(period="5d", interval="1d")
        if len(daily) < 2:
            return None
        prior = daily.iloc[-2]
        h, l, c = float(prior["High"]), float(prior["Low"]), float(prior["Close"])
    except Exception:
        return None
    return pivots_from_hlc(h, l, c)


def pivots_from_hlc(h: float, l: float, c: float) -> Pivots:
    p = (h + l + c) / 3
    r1 = 2 * p - l
    s1 = 2 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    return Pivots(pivot=p, r1=r1, r2=r2, s1=s1, s2=s2, prior_high=h, prior_low=l, prior_close=c)


def get_opening_range() -> OpeningRange | None:
    """High/low of the first ORB_MINUTES of today's regular session (9:30 ET)."""
    now = _now_et()
    session_open = now.replace(hour=RTH_OPEN[0], minute=RTH_OPEN[1], second=0, microsecond=0)
    range_end = session_open + timedelta(minutes=ORB_MINUTES)

    if now < session_open:
        return OpeningRange(high=0.0, low=0.0, formed=False)

    try:
        data = yf.Ticker(TICKER).history(period="1d", interval="1m")
        if data.empty:
            return None
        data.index = data.index.tz_convert(ET)
        window = data[(data.index >= session_open) & (data.index < range_end)]
        if window.empty:
            return OpeningRange(high=0.0, low=0.0, formed=False)
        return OpeningRange(
            high=float(window["High"].max()),
            low=float(window["Low"].min()),
            formed=now >= range_end,
        )
    except Exception:
        return None
