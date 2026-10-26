"""Contract specs and quarterly rollover for Nasdaq-100 futures (NQ / MNQ).

CME equity index futures list March, June, September and December contracts that expire on the
third Friday of the month. Volume moves to the next contract about a week earlier, so we roll on
the conventional roll date: 8 calendar days before expiration (the Thursday of the prior week).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

QUARTERLY_MONTHS = (3, 6, 9, 12)
MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}


@dataclass(frozen=True)
class Instrument:
    root: str
    tick_size: float
    point_value: float      # dollars per 1.00 index point, per contract
    commission_rt: float    # all-in round-trip estimate per contract (broker + exchange + NFA)

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 2)


# Commission figures are typical retail all-in estimates. Replace them with your broker's
# actual schedule before relying on any backtest.
NQ = Instrument(root="NQ", tick_size=0.25, point_value=20.0, commission_rt=4.50)
MNQ = Instrument(root="MNQ", tick_size=0.25, point_value=2.0, commission_rt=1.50)
INSTRUMENTS = {i.root: i for i in (NQ, MNQ)}


@dataclass(frozen=True)
class Contract:
    root: str
    year: int
    month: int
    expiration: date
    roll_date: date

    @property
    def symbol(self) -> str:
        """Exchange-style symbol, e.g. NQZ6 (single year digit, as Tradovate and TradingView use)."""
        return f"{self.root}{MONTH_CODES[self.month]}{self.year % 10}"


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # Friday == 4
    return first + timedelta(days=offset + 14)


def contract_for(root: str, year: int, month: int) -> Contract:
    if month not in QUARTERLY_MONTHS:
        raise ValueError(f"{root} only lists quarterly months, not {month}")
    exp = third_friday(year, month)
    return Contract(root=root, year=year, month=month, expiration=exp, roll_date=exp - timedelta(days=8))


def front_contract(root: str, on: date) -> Contract:
    """The contract to trade on a given date: the nearest one whose roll date hasn't arrived yet."""
    year = on.year
    while True:
        for month in QUARTERLY_MONTHS:
            c = contract_for(root, year, month)
            if on < c.roll_date:
                return c
        year += 1


def next_contract(c: Contract) -> Contract:
    i = QUARTERLY_MONTHS.index(c.month)
    if i == len(QUARTERLY_MONTHS) - 1:
        return contract_for(c.root, c.year + 1, QUARTERLY_MONTHS[0])
    return contract_for(c.root, c.year, QUARTERLY_MONTHS[i + 1])
