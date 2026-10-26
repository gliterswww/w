"""Risk manager and budget manager. Every entry must pass here; risk overrides signals.

Limits come from config/risk.json, which only a human edits. The engine loads them read-only at
startup and logs their hash. Nothing in the system writes to that file or raises a limit.

Position size depends only on the per-trade risk budget and the stop distance, never on recent
losses (no martingale, no "make it back" sizing).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from .instruments import Instrument


class RiskLimits(BaseModel):
    model_config = {"extra": "forbid"}

    account_capital: float = Field(25_000, gt=0, description="Capital the system is allowed to trade")
    risk_per_trade_usd: float = Field(150, gt=0)
    max_daily_loss_usd: float = Field(400, gt=0)
    max_weekly_loss_usd: float = Field(1_000, gt=0)
    max_monthly_loss_usd: float = Field(2_000, gt=0)
    max_total_drawdown_usd: float = Field(2_500, gt=0, description="From the equity high-water mark")
    max_open_positions: int = Field(1, ge=1, le=5)
    max_contracts: int = Field(2, ge=1, le=50)
    max_order_size: int = Field(2, ge=1, le=50)
    max_trades_per_day: int = Field(4, ge=1, le=50)
    max_consecutive_losses: int = Field(3, ge=1, le=20)
    max_slippage_ticks: float = Field(4, ge=0)
    daily_profit_lock_usd: float | None = Field(None, gt=0, description="Stop for the day once up this much")
    session_start: time = time(9, 30)
    no_entries_after: time = time(15, 30)
    flatten_at: time = time(15, 55)
    news_blackout_minutes_before: int = Field(5, ge=0, le=120)
    news_blackout_minutes_after: int = Field(10, ge=0, le=240)
    max_data_staleness_seconds: int = Field(120, ge=5)

    @field_validator("risk_per_trade_usd")
    @classmethod
    def _risk_sane(cls, v, info):
        cap = info.data.get("account_capital")
        if cap and v > 0.03 * cap:
            raise ValueError(f"risk_per_trade_usd {v} is over 3% of account_capital {cap}")
        return v

    @model_validator(mode="after")
    def _ordering(self):
        if not self.max_daily_loss_usd <= self.max_weekly_loss_usd <= self.max_monthly_loss_usd:
            raise ValueError("loss budgets must satisfy daily <= weekly <= monthly")
        if self.max_order_size > self.max_contracts:
            raise ValueError("max_order_size cannot exceed max_contracts")
        if not self.session_start < self.no_entries_after < self.flatten_at:
            raise ValueError("need session_start < no_entries_after < flatten_at")
        return self

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]


def load_limits(path: str | Path) -> RiskLimits:
    p = Path(path)
    if not p.exists():
        return RiskLimits()
    return RiskLimits.model_validate(json.loads(p.read_text()))


@dataclass
class ClosedTrade:
    day: date
    pnl_usd: float


@dataclass
class SystemHealth:
    """What the engine has *confirmed* about the outside world. Unknown == unsafe."""
    broker_connected: bool = False
    positions_reconciled: bool = False
    protective_orders_confirmed: bool = True   # True when flat
    data_fresh: bool = False
    contract_ok: bool = False


@dataclass
class Decision:
    allowed: bool
    qty: int = 0
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, limits: RiskLimits, instrument: Instrument):
        self.limits = limits
        self.instrument = instrument
        self.history: list[ClosedTrade] = []
        self.equity_peak = limits.account_capital
        self.lockout: str | None = None
        self.lockout_day: date | None = None

    # ---- bookkeeping ---------------------------------------------------------

    def record_trade(self, day: date, pnl_usd: float) -> None:
        self.history.append(ClosedTrade(day, pnl_usd))
        self.equity_peak = max(self.equity_peak, self.equity())

    def equity(self, unrealized: float = 0.0) -> float:
        return self.limits.account_capital + sum(t.pnl_usd for t in self.history) + unrealized

    def pnl_since(self, start: date) -> float:
        return sum(t.pnl_usd for t in self.history if t.day >= start)

    def consecutive_losses(self, day: date | None = None) -> int:
        """Losing streak, counted within `day` only when given (the daily circuit breaker)."""
        n = 0
        for t in reversed(self.history):
            if day is not None and t.day != day:
                break
            if t.pnl_usd > 0:
                break
            n += 1
        return n

    def trades_on(self, day: date) -> int:
        return sum(1 for t in self.history if t.day == day)

    # ---- budget manager ------------------------------------------------------

    def budgets(self, today: date, unrealized: float = 0.0) -> dict:
        lim = self.limits
        week_start = date.fromordinal(today.toordinal() - today.weekday())
        month_start = today.replace(day=1)
        day_pnl = self.pnl_since(today) + unrealized
        week_pnl = self.pnl_since(week_start) + unrealized
        month_pnl = self.pnl_since(month_start) + unrealized
        dd = max(0.0, self.equity_peak - self.equity(unrealized))

        def row(name, pnl, limit):
            used = max(0.0, -pnl)
            return {"name": name, "pnl": round(pnl, 2), "limit": limit, "used": round(used, 2),
                    "remaining": round(max(0.0, limit - used), 2), "used_pct": round(min(1.0, used / limit), 4)}

        return {
            "capital": lim.account_capital,
            "equity": round(self.equity(unrealized), 2),
            "equity_peak": round(self.equity_peak, 2),
            "risk_per_trade": lim.risk_per_trade_usd,
            "rows": [
                row("Daily loss", day_pnl, lim.max_daily_loss_usd),
                row("Weekly loss", week_pnl, lim.max_weekly_loss_usd),
                row("Monthly loss", month_pnl, lim.max_monthly_loss_usd),
                {"name": "Drawdown from peak", "pnl": round(-dd, 2), "limit": lim.max_total_drawdown_usd,
                 "used": round(dd, 2), "remaining": round(max(0.0, lim.max_total_drawdown_usd - dd), 2),
                 "used_pct": round(min(1.0, dd / lim.max_total_drawdown_usd), 4)},
            ],
        }

    # ---- the gate ------------------------------------------------------------

    def position_size(self, stop_points: float) -> int:
        """Contracts such that a stop-out (plus slippage and commission) costs <= risk budget."""
        inst, lim = self.instrument, self.limits
        per_contract = (stop_points + lim.max_slippage_ticks * inst.tick_size) * inst.point_value + inst.commission_rt
        if per_contract <= 0:
            return 0
        return max(0, min(math.floor(lim.risk_per_trade_usd / per_contract), lim.max_order_size, lim.max_contracts))

    def check_lockouts(self, now: datetime, unrealized: float = 0.0) -> str | None:
        """Day-level lockouts. Once set, they persist for the rest of the day."""
        today = now.date()
        if self.lockout_day != today:
            self.lockout, self.lockout_day = None, today
        if self.lockout:
            return self.lockout
        b = self.budgets(today, unrealized)
        lim = self.limits
        for r in b["rows"]:
            if r["used"] >= r["limit"]:
                self.lockout = f"{r['name']} limit reached (${r['used']:.0f} of ${r['limit']:.0f})"
                return self.lockout
        if self.consecutive_losses(today) >= lim.max_consecutive_losses:
            self.lockout = f"{self.consecutive_losses(today)} consecutive losses today"
        elif self.trades_on(today) >= lim.max_trades_per_day:
            self.lockout = f"max {lim.max_trades_per_day} trades for the day"
        elif lim.daily_profit_lock_usd and b["rows"][0]["pnl"] >= lim.daily_profit_lock_usd:
            self.lockout = f"daily profit lock (+${b['rows'][0]['pnl']:.0f})"
        return self.lockout

    def check_entry(self, *, now: datetime, side: int, price: float, stop: float, open_positions: int,
                    health: SystemHealth, kill_switch: bool, news_blackout: str | None,
                    unrealized: float = 0.0) -> Decision:
        reasons: list[str] = []
        lim = self.limits
        if kill_switch:
            reasons.append("emergency stop engaged")
        if not health.broker_connected:
            reasons.append("broker not connected")
        if not health.positions_reconciled:
            reasons.append("positions not reconciled with broker")
        if not health.protective_orders_confirmed:
            reasons.append("protective stop not confirmed on existing position")
        if not health.data_fresh:
            reasons.append("market data stale or unreliable")
        if not health.contract_ok:
            reasons.append("contract not confirmed (rollover)")
        if lockout := self.check_lockouts(now, unrealized):
            reasons.append(f"locked out: {lockout}")
        t = now.time()
        if not (lim.session_start <= t < lim.no_entries_after):
            reasons.append(f"outside entry window {lim.session_start:%H:%M}-{lim.no_entries_after:%H:%M} ET")
        if news_blackout:
            reasons.append(f"news blackout: {news_blackout}")
        if open_positions >= lim.max_open_positions:
            reasons.append(f"max open positions ({lim.max_open_positions})")
        stop_points = (price - stop) * side
        if stop_points <= 0:
            reasons.append("stop is on the wrong side of the entry")
        qty = self.position_size(stop_points) if stop_points > 0 else 0
        if stop_points > 0 and qty == 0:
            reasons.append(f"stop {stop_points:.2f} pts too wide for ${lim.risk_per_trade_usd:.0f} risk budget")
        return Decision(allowed=not reasons, qty=qty if not reasons else 0, reasons=reasons)

    def check_fill_slippage(self, expected: float, filled: float, side: int) -> str | None:
        ticks = (filled - expected) * side / self.instrument.tick_size
        if ticks > self.limits.max_slippage_ticks:
            return f"slippage {ticks:.0f} ticks exceeds max {self.limits.max_slippage_ticks:.0f}"
        return None

    def force_lockout(self, now: datetime, reason: str) -> None:
        self.lockout, self.lockout_day = reason, now.date()
