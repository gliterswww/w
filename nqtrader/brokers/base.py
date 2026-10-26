"""The interface every broker adapter implements (paper, Tradovate, Interactive Brokers, ...).

The engine depends only on this interface. A live adapter must:
  * Return the broker's *actual* positions and working orders. Never echo back the engine's own
    view; reconciliation depends on an independent source of truth.
  * Treat client_id as an idempotency key: placing the same client_id twice must not create a
    second order.
  * Submit the protective stop and target together with the entry (bracket/OSO) where the broker
    supports it, so the stop exists at the exchange the moment the entry fills.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class BrokerError(Exception):
    pass


@dataclass
class BrokerPosition:
    symbol: str
    qty: int          # signed: + long, - short
    avg_price: float


@dataclass
class WorkingOrder:
    client_id: str
    symbol: str
    kind: str         # "stop" | "limit" | "market"
    side: int         # +1 buy, -1 sell
    qty: int
    price: float | None
    parent: str | None = None


@dataclass
class OrderAck:
    client_id: str
    accepted: bool
    broker_id: str | None = None
    reason: str | None = None


@dataclass
class Fill:
    client_id: str
    symbol: str
    side: int
    qty: int
    price: float
    kind: str        # "entry" | "stop" | "target" | "flatten"
    expected: float  # reference price, for slippage checks
    ts: str
    parent: str | None = None


class Broker(ABC):
    name = "abstract"
    is_live = False

    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def balance(self) -> float: ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def working_orders(self) -> list[WorkingOrder]: ...

    @abstractmethod
    def place_bracket(self, *, client_id: str, symbol: str, side: int, qty: int, ref_price: float,
                      stop: float, target: float | None) -> OrderAck:
        """Market entry plus an OCO stop-loss / take-profit, attached server-side."""

    @abstractmethod
    def place_stop(self, *, client_id: str, symbol: str, side: int, qty: int, stop: float,
                   parent: str | None = None) -> OrderAck: ...

    @abstractmethod
    def cancel(self, client_id: str) -> bool: ...

    @abstractmethod
    def flatten(self, symbol: str, reason: str) -> list[Fill]:
        """Cancel working orders and close the position at market.

        Closing fills may be returned here (if the broker confirms synchronously) or delivered
        later through poll_fills(), never both. The engine sends this once per position."""

    def poll_fills(self) -> list[Fill]:
        """Fills since the last call. Live adapters drain their websocket queue here."""
        return []
