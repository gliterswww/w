"""Simulated broker. Fills with exactly the backtester's rules (see fills.py).

It can also inject faults (rejections, a missing stop, disconnects) so the engine's recovery
paths can be tested.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from ..fills import CostModel, check_exit, market_fill
from .base import Broker, BrokerPosition, Fill, OrderAck, WorkingOrder


@dataclass
class _Bracket:
    client_id: str
    symbol: str
    side: int
    qty: int
    ref_price: float
    stop: float | None
    target: float | None
    filled: bool = False
    entry_price: float | None = None


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, cost: CostModel, starting_balance: float):
        self.cost = cost
        self._balance = starting_balance
        self._brackets: dict[str, _Bracket] = {}
        self._position: BrokerPosition | None = None
        self._fills: list[Fill] = []
        self._ids = itertools.count(1)
        self._last_close: float | None = None
        self._ts = ""
        # fault injection
        self.connected_flag = True
        self.reject_next: str | None = None
        self.drop_stop_next = False
        self._flatten_pending = False
        self._flatten_parent: str | None = None

    # ---- state ---------------------------------------------------------------

    def connected(self) -> bool:
        return self.connected_flag

    def balance(self) -> float:
        return round(self._balance, 2)

    def positions(self) -> list[BrokerPosition]:
        return [self._position] if self._position and self._position.qty else []

    def working_orders(self) -> list[WorkingOrder]:
        out = []
        for b in self._brackets.values():
            if not b.filled:
                out.append(WorkingOrder(b.client_id, b.symbol, "market", b.side, b.qty, None))
                continue
            if b.stop is not None:
                out.append(WorkingOrder(b.client_id + ":stop", b.symbol, "stop", -b.side, b.qty, b.stop, b.client_id))
            if b.target is not None:
                out.append(WorkingOrder(b.client_id + ":target", b.symbol, "limit", -b.side, b.qty, b.target, b.client_id))
        return out

    # ---- orders --------------------------------------------------------------

    def place_bracket(self, *, client_id, symbol, side, qty, ref_price, stop, target) -> OrderAck:
        if not self.connected_flag:
            return OrderAck(client_id, False, reason="not connected")
        if client_id in self._brackets:
            return OrderAck(client_id, True, broker_id=f"P{client_id}", reason="duplicate ignored")
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            return OrderAck(client_id, False, reason=reason)
        stop_price = None if self.drop_stop_next else stop
        self.drop_stop_next = False
        self._brackets[client_id] = _Bracket(client_id, symbol, side, qty, ref_price, stop_price, target)
        return OrderAck(client_id, True, broker_id=f"P{next(self._ids)}")

    def place_stop(self, *, client_id, symbol, side, qty, stop, parent=None) -> OrderAck:
        b = self._brackets.get(parent or "")
        if not self.connected_flag or b is None:
            return OrderAck(client_id, False, reason="no such position")
        b.stop = stop
        return OrderAck(client_id, True, broker_id=f"P{next(self._ids)}")

    def cancel(self, client_id: str) -> bool:
        b = self._brackets.get(client_id)
        if b and not b.filled:
            del self._brackets[client_id]
            return True
        return False

    def flatten(self, symbol: str, reason: str) -> list[Fill]:
        """Cancel everything and send a market close. Like a real broker, the fill arrives
        asynchronously (here: at the next bar's open, the backtester's exit rule)."""
        self._brackets.clear()  # cancels the working entry, stop and target
        if self._position and self._position.qty:
            self._flatten_pending = True
        return []

    def poll_fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out

    # ---- simulation ------------------------------------------------------------

    def on_bar(self, ts: str, o: float, h: float, l: float, c: float) -> None:
        """Advance the simulation by one bar: fill pending entries at the open, then exits."""
        self._ts = ts
        if self._flatten_pending and self._position:
            side = 1 if self._position.qty > 0 else -1
            price = market_fill(-side, o, self.cost)
            self._fills.append(self._close(self._flatten_parent, -side, abs(self._position.qty), price, "flatten", o))
        self._flatten_pending = False
        for b in list(self._brackets.values()):
            if not b.filled:
                b.entry_price = market_fill(b.side, o, self.cost)
                b.filled = True
                self._position = BrokerPosition(b.symbol, b.side * b.qty, b.entry_price)
                self._flatten_parent = b.client_id
                self._fills.append(Fill(b.client_id, b.symbol, b.side, b.qty, b.entry_price, "entry", b.ref_price, ts))
            if b.stop is not None or b.target is not None:
                hit = check_exit(b.side, b.stop, b.target, o, h, l, self.cost)
                if hit:
                    expected = b.stop if hit[0] == "stop" else b.target
                    self._fills.append(self._close(b.client_id, -b.side, b.qty, hit[1], hit[0], expected))
                    del self._brackets[b.client_id]
        self._last_close = c

    def _close(self, parent: str | None, side: int, qty: int, price: float, kind: str, expected: float) -> Fill:
        pos = self._position
        pos_side = 1 if pos.qty > 0 else -1
        self._balance += self.cost.pnl_usd(pos_side, pos.avg_price, price, qty)
        self._position = None
        return Fill(f"{parent}:{kind}", pos.symbol, side, qty, price, kind, expected, self._ts, parent=parent)
