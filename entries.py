"""Turns raw levels (pivots + opening range) into a short, ranked list of session entries."""
from __future__ import annotations

from dataclasses import dataclass, asdict

from market_data import Pivots, OpeningRange

BUFFER = 5.0  # points added beyond a breakout level before triggering, to avoid noise
ORB_LABEL = "15-min"


@dataclass
class Entry:
    label: str
    side: str          # "Long" or "Short"
    trigger: float
    stop: float
    note: str


def build_entries(pivots: Pivots | None, orb: OpeningRange | None, price: float | None) -> list[dict]:
    entries: list[Entry] = []

    if orb and orb.formed:
        entries.append(Entry(
            label="ORB Breakout",
            side="Long",
            trigger=round(orb.high + BUFFER, 2),
            stop=round(orb.low, 2),
            note=f"Breaks above the {ORB_LABEL} opening range high; stop back inside the range.",
        ))
        entries.append(Entry(
            label="ORB Breakout",
            side="Short",
            trigger=round(orb.low - BUFFER, 2),
            stop=round(orb.high, 2),
            note=f"Breaks below the {ORB_LABEL} opening range low; stop back inside the range.",
        ))

    if pivots:
        entries.append(Entry(
            label="Pivot Support",
            side="Long",
            trigger=round(pivots.s1, 2),
            stop=round(pivots.s2, 2),
            note="Reaction long off first support (S1); invalidated on a break of S2.",
        ))
        entries.append(Entry(
            label="Pivot Resistance",
            side="Short",
            trigger=round(pivots.r1, 2),
            stop=round(pivots.r2, 2),
            note="Reaction short off first resistance (R1); invalidated on a break of R2.",
        ))

    # Cap at 4, prioritizing ORB (session-specific) over static pivots when both are present.
    entries = entries[:4]
    return [asdict(e) for e in entries]
