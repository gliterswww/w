"""Performance statistics for a set of trades over a set of sessions."""
from __future__ import annotations

import math

import numpy as np

from .engine import Trade


def daily_pnl(per_session: list[list[Trade]]) -> np.ndarray:
    return np.array([sum(t.pnl_usd for t in day) for day in per_session], dtype=float)


def max_drawdown(equity_changes: np.ndarray) -> float:
    if len(equity_changes) == 0:
        return 0.0
    eq = np.concatenate([[0.0], np.cumsum(equity_changes)])
    return float(np.max(np.maximum.accumulate(eq) - eq))


def bootstrap_mean_ci(x: np.ndarray, n: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    if len(x) < 2:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def summarize(per_session: list[list[Trade]], session_bars: int = 390) -> dict:
    """Metrics over the given sessions (sessions with no trades still count, as flat days)."""
    trades = [t for day in per_session for t in day]
    days = daily_pnl(per_session)
    pnl = np.array([t.pnl_usd for t in trades], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    sd = float(days.std(ddof=1)) if len(days) > 1 else 0.0
    ci = bootstrap_mean_ci(pnl)
    t_stat = float(pnl.mean() / (pnl.std(ddof=1) / math.sqrt(len(pnl)))) if len(pnl) > 2 and pnl.std() > 0 else 0.0
    return {
        "sessions": len(per_session),
        "trades": len(trades),
        "net_pnl": round(float(pnl.sum()), 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "win_rate": round(len(wins) / len(pnl), 4) if len(pnl) else 0.0,
        "expectancy": round(float(pnl.mean()), 2) if len(pnl) else 0.0,
        "expectancy_ci95": [round(ci[0], 2), round(ci[1], 2)],
        "t_stat": round(t_stat, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "avg_r": round(float(np.mean([t.r_multiple for t in trades])), 3) if trades else 0.0,
        "max_drawdown": round(max_drawdown(days), 2),
        "sharpe_annual": round(float(days.mean() / sd * math.sqrt(252)), 2) if sd > 0 else 0.0,
        "trades_per_day": round(len(trades) / len(per_session), 3) if per_session else 0.0,
        "exposure": round(sum(t.bars_held for t in trades) / (session_bars * len(per_session)), 4)
        if per_session else 0.0,
    }
