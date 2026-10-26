"""Bar-by-bar backtester for single-position intraday strategies.

Timing model (no look-ahead):
  * A strategy sees bars 0..i only (arrays are sliced before it gets them) and may emit a signal
    at the *close* of bar i.
  * The entry is a market order filled at the *open* of bar i+1, plus slippage.
  * Stops and targets are then checked on each following bar's high/low (see fills.py).
  * Positions are flattened at 15:55 ET or the session's last bar, whichever is first.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date

import numpy as np

from ..data import Session, session_vwap
from ..fills import CostModel, check_exit, market_fill

FLATTEN_MINUTE = 15 * 60 + 55


@dataclass
class Signal:
    side: int                     # +1 long, -1 short
    stop: float                   # absolute price
    target: float | None = None   # absolute price; None = hold to time exit
    exit_minute: int | None = None
    tag: str = ""


@dataclass
class Trade:
    strategy: str
    day: date
    side: int
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    target: float | None
    reason: str
    risk_points: float
    pnl_points: float
    pnl_usd: float
    bars_held: int
    tag: str = ""

    @property
    def r_multiple(self) -> float:
        return self.pnl_points / self.risk_points if self.risk_points > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["day"] = self.day.isoformat()
        d["r_multiple"] = round(self.r_multiple, 3)
        return d


class View:
    """What a strategy is allowed to see at bar i: everything up to and including bar i."""

    __slots__ = ("i", "s", "open", "high", "low", "close", "volume", "minute", "vwap", "vwap_sd")

    def __init__(self, s: Session, i: int, vwap: np.ndarray, vwap_sd: np.ndarray):
        n = i + 1
        self.i = i
        self.s = s
        self.open, self.high, self.low = s.open[:n], s.high[:n], s.low[:n]
        self.close, self.volume, self.minute = s.close[:n], s.volume[:n], s.minute[:n]
        self.vwap, self.vwap_sd = vwap[:n], vwap_sd[:n]

    @property
    def atr(self) -> float:
        return self.s.atr


class Strategy:
    """Subclass and implement on_bar. Keep any per-session state in reset()."""

    name = "base"
    max_trades_per_day = 1
    entry_start = 9 * 60 + 31
    entry_end = 15 * 60

    def __init__(self, **params):
        self.params = params

    @classmethod
    def param_grid(cls) -> list[dict]:
        return [{}]

    def reset(self, s: Session) -> None:
        pass

    def on_bar(self, v: View) -> Signal | None:
        raise NotImplementedError

    @property
    def key(self) -> str:
        if not self.params:
            return self.name
        return self.name + "(" + ",".join(f"{k}={v}" for k, v in sorted(self.params.items())) + ")"


@dataclass
class BacktestConfig:
    cost: CostModel
    skip_days: frozenset = field(default_factory=frozenset)   # e.g. news-blackout days
    flatten_minute: int = FLATTEN_MINUTE


def run_session(strategy: Strategy, s: Session, cfg: BacktestConfig) -> list[Trade]:
    return simulate(strategy, s, cfg)[0]


def simulate(strategy: Strategy, s: Session, cfg: BacktestConfig, live: bool = False):
    """Returns (trades, signals, open_position, pending_signal).

    With live=True the session is treated as still in progress: a signal on the last bar is
    returned as pending, and an open position is left open rather than closed at the last bar.
    """
    signals: list[dict] = []
    if s.day in cfg.skip_days or np.isnan(s.atr):
        return [], signals, None, None
    strategy.reset(s)
    vwap, vwap_sd = session_vwap(s)
    trades: list[Trade] = []
    n = len(s)
    pending: Signal | None = None
    pos = None  # (side, entry, stop, target, exit_minute, entry_idx, tag, signal_close)

    def close_pos(i: int, price: float, reason: str) -> None:
        nonlocal pos
        side, entry, stop, target, _, ei, tag, risk = pos
        pnl_pts = (price - entry) * side
        trades.append(Trade(
            strategy=strategy.key, day=s.day, side=side,
            entry_time=str(s.ts[ei])[11:16], exit_time=str(s.ts[i])[11:16],
            entry=round(entry, 2), exit=round(price, 2), stop=stop, target=target, reason=reason,
            risk_points=round(risk, 2), pnl_points=round(pnl_pts, 2),
            pnl_usd=round(cfg.cost.pnl_usd(side, entry, price), 2), bars_held=i - ei, tag=tag,
        ))
        pos = None

    for i in range(n):
        o, h, l, c, m = s.open[i], s.high[i], s.low[i], s.close[i], s.minute[i]

        if pending is not None:
            entry = market_fill(pending.side, o, cfg.cost)
            pos = (pending.side, entry, pending.stop, pending.target, pending.exit_minute, i,
                   pending.tag, abs(entry - pending.stop))
            pending = None

        if pos is not None:
            exit_minute = min(pos[4] or cfg.flatten_minute, cfg.flatten_minute)
            if m >= exit_minute:
                close_pos(i, market_fill(-pos[0], o, cfg.cost), "time")
            else:
                hit = check_exit(pos[0], pos[2], pos[3], o, h, l, cfg.cost)
                if hit:
                    close_pos(i, hit[1], hit[0])

        if (pos is None and pending is None and (live or i < n - 1)
                and len(trades) < strategy.max_trades_per_day
                and strategy.entry_start <= m < min(strategy.entry_end, cfg.flatten_minute)):
            sig = strategy.on_bar(View(s, i, vwap, vwap_sd))
            if sig is not None:
                sig = _to_ticks(sig, cfg.cost.instrument)  # exchanges only accept tick prices
            if sig is not None and _valid(sig, c):
                pending = sig
                signals.append({"time": str(s.ts[i])[11:16], "side": sig.side, "price": round(float(c), 2),
                                "stop": round(sig.stop, 2),
                                "target": None if sig.target is None else round(sig.target, 2), "tag": sig.tag})

    if live:
        open_pos = None
        if pos is not None:
            open_pos = {"side": pos[0], "entry": round(pos[1], 2), "stop": pos[2], "target": pos[3],
                        "since": str(s.ts[pos[5]])[11:16]}
        return trades, signals, open_pos, pending
    if pos is not None:
        close_pos(n - 1, market_fill(-pos[0], s.close[n - 1], cfg.cost), "eod")
    return trades, signals, None, None


def _to_ticks(sig: Signal, inst) -> Signal:
    return Signal(side=sig.side, stop=inst.round_to_tick(sig.stop),
                  target=None if sig.target is None else inst.round_to_tick(sig.target),
                  exit_minute=sig.exit_minute, tag=sig.tag)


def _valid(sig: Signal, price: float) -> bool:
    if sig.side not in (1, -1) or not np.isfinite(sig.stop):
        return False
    if (price - sig.stop) * sig.side <= 0:          # stop must be on the losing side
        return False
    if sig.target is not None and (sig.target - price) * sig.side <= 0:
        return False
    return True


def run_backtest(strategy: Strategy, sessions: list[Session], cfg: BacktestConfig) -> list[list[Trade]]:
    """Trades for every session, as a list aligned with `sessions`."""
    return [run_session(strategy, s, cfg) for s in sessions]
