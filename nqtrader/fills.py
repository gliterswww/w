"""Fill model shared by the backtester and the paper broker, so both execute identically.

Conservative by design:
  * Market and stop orders pay `slippage_ticks` against us.
  * Gaps through a stop fill at the bar open (worse than the stop), never at the stop.
  * Profit targets (limit orders) need price to trade one tick *through* the level to count as
    filled, since touching a limit doesn't guarantee a fill when you're at the back of the queue.
  * If one bar reaches both the stop and the target, the stop is assumed to have filled first.
"""
from __future__ import annotations

from dataclasses import dataclass

from .instruments import Instrument


@dataclass(frozen=True)
class CostModel:
    instrument: Instrument
    slippage_ticks: float = 1.0
    commission_rt: float | None = None  # None -> instrument default

    @property
    def slippage(self) -> float:
        return self.slippage_ticks * self.instrument.tick_size

    @property
    def commission(self) -> float:
        return self.instrument.commission_rt if self.commission_rt is None else self.commission_rt

    def pnl_usd(self, side: int, entry: float, exit: float, qty: int = 1) -> float:
        return (exit - entry) * side * qty * self.instrument.point_value - self.commission * qty


def market_fill(side: int, ref_price: float, cost: CostModel) -> float:
    """Buy (side=+1) or sell (-1) at ref_price, paying slippage."""
    return ref_price + side * cost.slippage


def check_exit(side: int, stop: float | None, target: float | None, o: float, h: float, l: float,
               cost: CostModel) -> tuple[str, float] | None:
    """Exit for an open position over one bar, or None. side is the position's side."""
    tick = cost.instrument.tick_size
    if side > 0:
        if stop is not None and l <= stop:
            return "stop", min(stop, o) - cost.slippage
        if target is not None and (o >= target or h >= target + tick):
            return "target", max(target, o)
    else:
        if stop is not None and h >= stop:
            return "stop", max(stop, o) + cost.slippage
        if target is not None and (o <= target or l <= target - tick):
            return "target", min(target, o)
    return None
