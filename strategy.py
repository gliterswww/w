"""Rules-based session trader for NQ: trades the dashboard's entries with fixed risk limits.

The same engine drives both the backtest and the live paper bot, so a strategy that looks good
in backtest behaves identically when it runs for real. Feed it 1-minute bars in order via on_bar().
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, time

from entries import build_entries
from market_data import OpeningRange, Pivots, RTH_OPEN, ORB_MINUTES


@dataclass
class Config:
    # MNQ (micro) is $2/point; full-size NQ is $20/point. Start with micros.
    symbol: str = "MNQ"
    point_value: float = 2.0
    contracts: int = 1
    commission: float = 1.50          # round trip, per contract, dollars
    reward_risk: float = 2.0          # take profit at this multiple of the stop distance
    max_risk_points: float = 60.0     # skip any setup whose stop is further away than this
    max_trades_per_day: int = 3
    max_daily_loss: float = 250.0     # dollars; no new trades once realized P&L hits -this
    last_entry: time = time(15, 0)    # no new entries after this (ET)
    flatten_at: time = time(15, 55)   # close any open position at this time (ET)


@dataclass
class Bar:
    ts: datetime  # bar open time, ET
    open: float
    high: float
    low: float
    close: float


@dataclass
class Setup:
    label: str
    side: str      # "Long" / "Short"
    kind: str      # "breakout" / "reaction"
    trigger: float
    stop: float
    target: float
    armed: bool = False  # price must first be on the waiting side of the trigger
    done: bool = False   # each setup trades at most once per session


@dataclass
class Position:
    label: str
    side: str
    entry: float
    stop: float
    target: float
    qty: int
    opened_at: str


@dataclass
class Trade:
    label: str
    side: str
    qty: int
    entry: float
    exit: float
    opened_at: str
    closed_at: str
    reason: str
    pnl: float


class SessionTrader:
    """Trades one regular session (9:30-16:00 ET). Create a new one each day."""

    def __init__(self, pivots: Pivots | None, config: Config | None = None):
        self.cfg = config or Config()
        self.pivots = pivots
        self.setups: dict[tuple[str, str], Setup] = {}
        self.position: Position | None = None
        self.trades: list[Trade] = []
        self.log: list[str] = []
        self.halted: str | None = None
        self._orb_high: float | None = None
        self._orb_low: float | None = None
        self._orb_formed = False
        self._last_close: float | None = None

    # ---- public -------------------------------------------------------------

    @property
    def realized_pnl(self) -> float:
        return round(sum(t.pnl for t in self.trades), 2)

    def on_bar(self, bar: Bar) -> None:
        t = bar.ts.time()
        session_open = time(*RTH_OPEN)
        if t < session_open or t >= time(16, 0):
            return

        if not self.setups and not self._orb_formed:
            self._add_setups()  # pivot setups are known from the open

        self._update_opening_range(bar)

        if self.position:
            self._manage_position(bar)
        elif self._can_enter(t):
            self._try_entries(bar)

        if self.position and t >= self.cfg.flatten_at:
            self._close(bar.close, bar.ts, "end of day")

        self._arm_setups(bar.close)
        self._last_close = bar.close

    def status(self) -> dict:
        return {
            "position": asdict(self.position) if self.position else None,
            "trades": [asdict(t) for t in self.trades],
            "realized_pnl": self.realized_pnl,
            "halted": self.halted,
            "setups": [
                {"label": s.label, "side": s.side, "trigger": s.trigger, "stop": s.stop,
                 "target": s.target, "armed": s.armed, "done": s.done}
                for s in self.setups.values()
            ],
            "log": self.log[-50:],
        }

    # ---- internals ----------------------------------------------------------

    def _update_opening_range(self, bar: Bar) -> None:
        if self._orb_formed:
            return
        minutes_in = (bar.ts.hour * 60 + bar.ts.minute) - (RTH_OPEN[0] * 60 + RTH_OPEN[1])
        if minutes_in < ORB_MINUTES:
            self._orb_high = bar.high if self._orb_high is None else max(self._orb_high, bar.high)
            self._orb_low = bar.low if self._orb_low is None else min(self._orb_low, bar.low)
        elif self._orb_high is not None:
            self._orb_formed = True
            self._add_setups()

    def _add_setups(self) -> None:
        orb = None
        if self._orb_formed:
            orb = OpeningRange(high=self._orb_high, low=self._orb_low, formed=True)
        for e in build_entries(self.pivots, orb, self._last_close):
            key = (e["label"], e["side"])
            if key in self.setups:
                continue
            risk = abs(e["trigger"] - e["stop"])
            if risk <= 0:
                continue
            direction = 1 if e["side"] == "Long" else -1
            setup = Setup(
                label=e["label"], side=e["side"], kind=e["kind"],
                trigger=e["trigger"], stop=e["stop"],
                target=round(e["trigger"] + direction * risk * self.cfg.reward_risk, 2),
            )
            if risk > self.cfg.max_risk_points:
                setup.done = True
                self._note(f"Skipping {e['label']} {e['side']}: stop {risk:.1f} pts away "
                           f"(max {self.cfg.max_risk_points:.0f})")
            self.setups[key] = setup

    def _arm_setups(self, price: float) -> None:
        """A setup only goes live once price is on the side it's waiting from.

        e.g. a breakout long above 18000 isn't live while price is already at 18050 (we missed it),
        and a reaction long at support isn't live while price is already below support.
        """
        for s in self.setups.values():
            if s.done or s.armed:
                continue
            waiting_below = (s.side == "Long") == (s.kind == "breakout")
            if (waiting_below and price < s.trigger) or (not waiting_below and price > s.trigger):
                s.armed = True

    def _can_enter(self, t: time) -> bool:
        if self.halted:
            return False
        if self.realized_pnl <= -self.cfg.max_daily_loss:
            self.halted = f"daily loss limit hit ({self.realized_pnl:.2f})"
        elif len(self.trades) >= self.cfg.max_trades_per_day:
            self.halted = f"max {self.cfg.max_trades_per_day} trades for the day"
        if self.halted:
            self._note(f"Done for the day: {self.halted}")
            return False
        return t < self.cfg.last_entry

    def _try_entries(self, bar: Bar) -> None:
        for s in self.setups.values():
            if s.done or not s.armed:
                continue
            fill = self._entry_fill(s, bar)
            if fill is None:
                continue
            s.done = True
            self.position = Position(
                label=s.label, side=s.side, entry=fill, stop=s.stop, target=s.target,
                qty=self.cfg.contracts, opened_at=bar.ts.isoformat(),
            )
            self._note(f"ENTER {s.side} {self.cfg.contracts} {self.cfg.symbol} @ {fill:.2f} "
                       f"({s.label}) stop {s.stop:.2f} target {s.target:.2f}")
            # Conservative: if the entry bar also reached the stop, assume we got stopped.
            self._manage_position(bar, stop_only=True)
            return

    @staticmethod
    def _entry_fill(s: Setup, bar: Bar) -> float | None:
        buy = s.side == "Long"
        if s.kind == "breakout":  # stop order: buy above / sell below; gaps fill at the open
            if buy and bar.high >= s.trigger:
                return max(s.trigger, bar.open)
            if not buy and bar.low <= s.trigger:
                return min(s.trigger, bar.open)
        else:                     # limit order: buy at/below / sell at/above
            if buy and bar.low <= s.trigger:
                return min(s.trigger, bar.open)
            if not buy and bar.high >= s.trigger:
                return max(s.trigger, bar.open)
        return None

    def _manage_position(self, bar: Bar, stop_only: bool = False) -> None:
        p = self.position
        if p.side == "Long":
            if bar.low <= p.stop:
                self._close(min(p.stop, bar.open), bar.ts, "stop")
            elif not stop_only and bar.high >= p.target:
                self._close(max(p.target, bar.open), bar.ts, "target")
        else:
            if bar.high >= p.stop:
                self._close(max(p.stop, bar.open), bar.ts, "stop")
            elif not stop_only and bar.low <= p.target:
                self._close(min(p.target, bar.open), bar.ts, "target")

    def _close(self, price: float, ts: datetime, reason: str) -> None:
        p = self.position
        direction = 1 if p.side == "Long" else -1
        pnl = (price - p.entry) * direction * p.qty * self.cfg.point_value - self.cfg.commission * p.qty
        self.trades.append(Trade(
            label=p.label, side=p.side, qty=p.qty, entry=p.entry, exit=round(price, 2),
            opened_at=p.opened_at, closed_at=ts.isoformat(), reason=reason, pnl=round(pnl, 2),
        ))
        self.position = None
        self._note(f"EXIT {p.side} @ {price:.2f} ({reason}) P&L {pnl:+.2f}  day {self.realized_pnl:+.2f}")

    def _note(self, msg: str) -> None:
        self.log.append(msg)
