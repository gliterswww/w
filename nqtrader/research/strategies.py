"""Candidate NQ strategies and the baselines they must beat.

Each strategy has a deliberately small parameter grid. A big grid makes some combination look
great by luck alone, and walk-forward selection can only partly correct for that.

"atr" below means the average RTH range of the previous 14 sessions, which is known before the
open. Stops and targets are scaled by it so they adapt to volatility.
"""
from __future__ import annotations

import hashlib
import itertools

import numpy as np

from ..data import Session
from .engine import Signal, Strategy, View

M = lambda h, m: h * 60 + m  # noqa: E731  minutes since midnight


def _grid(**axes) -> list[dict]:
    keys = list(axes)
    return [dict(zip(keys, vals)) for vals in itertools.product(*axes.values())]


class OpeningRangeBreakout(Strategy):
    """Close beyond the first N minutes' range -> trade the break. N=30 is the "10 AM range".

    Stop: the opposite side of the range, capped at 0.35 ATR. Target: target_r x risk, or hold
    to the 15:55 flatten when target_r == 0.
    """
    name = "orb"
    entry_end = M(12, 0)

    @classmethod
    def param_grid(cls):
        return _grid(minutes=[15, 30, 60], target_r=[1.0, 2.0, 0])

    def reset(self, s: Session):
        self.hi = self.lo = None

    def on_bar(self, v: View):
        n = self.params["minutes"]
        end = M(9, 30) + n
        if v.minute[-1] < end - 1:
            return None
        if self.hi is None:
            k = int(np.searchsorted(v.minute, end))
            self.hi, self.lo = float(v.high[:k].max()), float(v.low[:k].min())
            if v.minute[-1] < end:  # this bar completed the range; trade from the next one
                return None
        c = float(v.close[-1])
        cap = 0.35 * v.atr
        for side, level, other in ((1, self.hi, self.lo), (-1, self.lo, self.hi)):
            if (c - level) * side > 0:
                stop = other if abs(c - other) <= cap else c - side * cap
                return _with_target(side, c, stop, self.params["target_r"], tag=f"orb{n}")
        return None


class VWAPTrendPullback(Strategy):
    """With VWAP sloping, buy a pullback that tags VWAP and closes back on the trend side."""
    name = "vwap_pullback"
    max_trades_per_day = 2
    entry_start = M(10, 0)
    entry_end = M(14, 30)

    @classmethod
    def param_grid(cls):
        return _grid(stop_atr=[0.1, 0.2], target_r=[1.5, 3.0])

    def on_bar(self, v: View):
        if v.i < 30:
            return None
        vw, c, atr = v.vwap, float(v.close[-1]), v.atr
        slope = vw[-1] - vw[-31]
        if abs(slope) < 0.03 * atr:
            return None
        side = 1 if slope > 0 else -1
        touched = v.low[-1] <= vw[-1] if side > 0 else v.high[-1] >= vw[-1]
        if touched and (c - vw[-1]) * side > 0:
            return _with_target(side, c, c - side * self.params["stop_atr"] * atr, self.params["target_r"])
        return None


class VWAPReversion(Strategy):
    """Fade a stretch beyond VWAP +/- k standard deviations, targeting VWAP."""
    name = "vwap_reversion"
    max_trades_per_day = 2
    entry_start = M(10, 0)

    @classmethod
    def param_grid(cls):
        return _grid(k=[2.0, 2.5, 3.0], stop_atr=[0.1, 0.2])

    def on_bar(self, v: View):
        if v.i < 30:
            return None
        c, vw, sd = float(v.close[-1]), float(v.vwap[-1]), float(v.vwap_sd[-1])
        if sd <= 0:
            return None
        z = (c - vw) / sd
        if abs(z) < self.params["k"]:
            return None
        side = -1 if z > 0 else 1
        return Signal(side=side, stop=c - side * self.params["stop_atr"] * v.atr, target=vw, tag=f"z{z:+.1f}")


class FailedBreakout(Strategy):
    """Liquidity sweep: price runs a key level, then closes back inside -> trade the reversal.

    Levels are the 30-minute opening range or the prior day's high/low. Stop goes just beyond
    the sweep extreme.
    """
    name = "failed_breakout"
    entry_start = M(10, 0)
    entry_end = M(14, 0)

    @classmethod
    def param_grid(cls):
        return _grid(levels=["orb30", "prior_day"], target_r=[1.0, 2.0], reclaim_bars=[3, 10])

    def reset(self, s: Session):
        self.swept: dict = {}  # (side, level) -> (bar index of sweep, extreme)
        self.dead: set = set()  # levels that broke and held, or already traded

    def on_bar(self, v: View):
        if self.params["levels"] == "orb30":
            k = int(np.searchsorted(v.minute, M(10, 0)))
            hi, lo = float(v.high[:k].max()), float(v.low[:k].min())
        else:
            hi, lo = v.s.prev_high, v.s.prev_low
        h, l, c = float(v.high[-1]), float(v.low[-1]), float(v.close[-1])
        buffer = 0.02 * v.atr
        for side, level in ((-1, hi), (1, lo)):  # sweep of a high -> short, of a low -> long
            key = (side, level)
            if key in self.dead or not np.isfinite(level):
                continue
            beyond = (h - level) if side < 0 else (level - l)
            if beyond >= buffer:
                prev = self.swept.get(key)
                extreme = h if side < 0 else l
                if prev is None:
                    self.swept[key] = (v.i, extreme)
                else:
                    self.swept[key] = (prev[0], max(prev[1], extreme) if side < 0 else min(prev[1], extreme))
            sw = self.swept.get(key)
            if sw is None:
                continue
            if v.i - sw[0] > self.params["reclaim_bars"]:
                del self.swept[key]  # it held beyond the level: a real breakout, not a sweep
                self.dead.add(key)
                continue
            back_inside = c < level if side < 0 else c > level
            if back_inside:
                stop = sw[1] - side * buffer
                del self.swept[key]
                self.dead.add(key)
                return _with_target(side, c, stop, self.params["target_r"], tag=f"sweep_{self.params['levels']}")
        return None


class IntradayMomentum(Strategy):
    """Late-day continuation: if the session has moved decisively, ride it into the close.

    Grounded in the published "intraday momentum" effect (Gao, Han, Li & Zhou, 2018): early-session
    returns have predicted last-half-hour returns in index futures.
    """
    name = "intraday_momentum"
    entry_start = M(15, 29)
    entry_end = M(15, 31)

    @classmethod
    def param_grid(cls):
        return _grid(window=["first30", "day"], min_move_atr=[0.0, 0.15])

    def on_bar(self, v: View):
        if v.minute[-1] != M(15, 29):
            return None
        o = float(v.open[0])
        ref = float(v.close[int(np.searchsorted(v.minute, M(9, 59)))]) if self.params["window"] == "first30" \
            else float(v.close[-1])
        move = ref - o
        if abs(move) < self.params["min_move_atr"] * v.atr or move == 0:
            return None
        side = 1 if move > 0 else -1
        c = float(v.close[-1])
        return Signal(side=side, stop=c - side * 0.25 * v.atr, target=None, tag=self.params["window"])


# --------------------------------------------------------------------------- baselines


class NoTrade(Strategy):
    """The benchmark every strategy must beat after costs: doing nothing."""
    name = "baseline_no_trade"

    def on_bar(self, v):
        return None


class RandomEntry(Strategy):
    """One random-direction trade per day at a random time, with typical stop/target sizes.

    Deterministic per day (seeded by date) so results are reproducible. A strategy that can't
    beat this has no skill at picking entries.
    """
    name = "baseline_random"
    entry_start = M(10, 0)
    entry_end = M(14, 30)

    @classmethod
    def param_grid(cls):
        return [{"seed": k} for k in range(5)]

    def reset(self, s: Session):
        h = int(hashlib.sha256(f"{s.day}-{self.params.get('seed', 0)}".encode()).hexdigest(), 16)
        self.at = self.entry_start + h % (self.entry_end - self.entry_start)
        self.side = 1 if (h >> 32) & 1 else -1

    def on_bar(self, v):
        if v.minute[-1] != self.at:
            return None
        c = float(v.close[-1])
        return _with_target(self.side, c, c - self.side * 0.15 * v.atr, 1.5)


class SessionDrift(Strategy):
    """Buy the open, sell the 15:55 flatten: captures the market's own intraday drift."""
    name = "baseline_long_session"
    entry_start = M(9, 30)

    def on_bar(self, v):
        c = float(v.close[-1])
        return Signal(side=1, stop=c - 1.0 * v.atr, target=None)


def _with_target(side: int, price: float, stop: float, target_r: float, tag: str = "") -> Signal:
    risk = abs(price - stop)
    target = price + side * risk * target_r if target_r else None
    return Signal(side=side, stop=stop, target=target, tag=tag)


CANDIDATES: list[type[Strategy]] = [
    OpeningRangeBreakout, VWAPTrendPullback, VWAPReversion, FailedBreakout, IntradayMomentum,
]
BASELINES: list[type[Strategy]] = [NoTrade, RandomEntry, SessionDrift]
ALL = {cls.name: cls for cls in CANDIDATES + BASELINES}
