"""Synthetic NQ-like 1-minute bars for testing the research pipeline.

A pure random walk has no edge by construction, so every strategy should fail validation on it.
That makes it a test of the pipeline itself: if anything "passes" on random data, the validation
is broken. With `edge="orb"`, a small drift is injected after opening-range breakouts so the tests
can also check that a real edge does get detected.

Synthetic data is never evidence about real markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import ET


def generate_bars(n_sessions: int = 500, seed: int = 7, start: str = "2023-01-02",
                  start_price: float = 15000.0, edge: str | None = None,
                  edge_strength: float = 0.06) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n_sessions)
    minutes = np.arange(390)
    # U-shaped intraday volatility and volume, as in real index futures
    u = 1.0 + 1.6 * np.exp(-minutes / 25) + 0.6 * np.exp(-(389 - minutes) / 20)
    frames = []
    price = start_price
    for day in days:
        daily_vol = rng.lognormal(mean=0.0, sigma=0.35)
        sigma = 0.00042 * daily_vol * u            # per-minute return std
        rets = rng.standard_normal(390) * sigma
        price *= np.exp(rng.standard_normal() * 0.004)  # overnight gap
        closes = np.empty(390)
        opens = np.empty(390)
        p = price
        orb_hi = orb_lo = None
        for m in range(390):
            opens[m] = p
            r = rets[m]
            if edge == "orb" and m >= 30:
                if orb_hi is None:
                    orb_hi, orb_lo = closes[:30].max(), closes[:30].min()
                if p > orb_hi:
                    r += edge_strength * sigma[m]
                elif p < orb_lo:
                    r -= edge_strength * sigma[m]
            p = p * np.exp(r)
            closes[m] = p
        wick = np.abs(rng.standard_normal((2, 390))) * sigma * price * 0.6
        highs = np.maximum(opens, closes) + wick[0]
        lows = np.minimum(opens, closes) - wick[1]
        vol = (rng.gamma(2.0, 400, 390) * u).round()
        idx = pd.date_range(pd.Timestamp(day).replace(hour=9, minute=30), periods=390, freq="1min", tz=ET)
        frames.append(pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                                    "volume": vol}, index=idx))
        price = p
    return _round_ticks(pd.concat(frames))


def _round_ticks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = (out[c] / 0.25).round() * 0.25
    out["high"] = out[["open", "high", "close"]].max(axis=1)
    out["low"] = out[["open", "low", "close"]].min(axis=1)
    return out
