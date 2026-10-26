"""Walk-forward validation, holdout testing, regime breakdown and sensitivity analysis.

Data discipline:
  * The most recent `holdout_frac` of sessions is a final holdout. Walk-forward never touches it.
  * Walk-forward: pick parameters on a rolling training window, trade them on the *next* window,
    and stitch together only those out-of-sample results.
  * Each parameter combination is backtested once over all sessions; training windows just read
    their slice. That's equivalent to re-running, because strategies carry no state across
    sessions and every feature they use is causal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..data import Session
from .engine import BacktestConfig, Strategy, Trade, run_backtest
from .metrics import daily_pnl, summarize

MIN_TRAIN_TRADES = 20


@dataclass
class WalkForwardConfig:
    train_sessions: int = 250
    test_sessions: int = 63
    holdout_frac: float = 0.2


def _objective(per_session: list[list[Trade]]) -> float:
    """Daily Sharpe of a training slice; untradeable if it has too few trades to judge."""
    if sum(len(d) for d in per_session) < MIN_TRAIN_TRADES:
        return -math.inf
    days = daily_pnl(per_session)
    sd = days.std(ddof=1)
    return float(days.mean() / sd) if sd > 0 else -math.inf


def regime_labels(sessions: list[Session]) -> list[dict]:
    """Ex-post labels per session, for attribution only (they use the session's own data).

    Volatility compares the session's range with the median of the prior 20 sessions; trend
    measures how much of the range the open-to-close move covered.
    """
    labels, ranges = [], []
    for s in sessions:
        r = s.range
        med = float(np.median(ranges[-20:])) if len(ranges) >= 5 else r
        vol = "high_vol" if r > 1.25 * med else "low_vol" if r < 0.8 * med else "normal_vol"
        eff = abs(s.close[-1] - s.open[0]) / r if r > 0 else 0.0
        trend = "trend_day" if eff >= 0.6 else "range_day" if eff <= 0.3 else "mixed_day"
        labels.append({"vol": vol, "trend": trend})
        ranges.append(r)
    return labels


def evaluate_strategy(cls: type[Strategy], sessions: list[Session], bt: BacktestConfig,
                      wf: WalkForwardConfig) -> dict:
    n = len(sessions)
    n_dev = int(n * (1 - wf.holdout_frac))
    grid = cls.param_grid()
    runs = {}
    for params in grid:
        strat = cls(**params)
        runs[strat.key] = (params, run_backtest(strat, sessions, bt))

    # Sensitivity: every combination over the development period (in-sample by definition).
    sensitivity = [
        {"key": key, "params": params, **_brief(summarize(per[:n_dev]))}
        for key, (params, per) in runs.items()
    ]

    folds, oos = [], []
    start = 0
    while start + wf.train_sessions + 1 <= n_dev:
        tr = slice(start, start + wf.train_sessions)
        te = slice(tr.stop, min(tr.stop + wf.test_sessions, n_dev))
        scored = [(_objective(per[tr]), key) for key, (_, per) in runs.items()]
        best_score, best_key = max(scored)
        if best_score == -math.inf or best_score <= 0:
            chosen, test_trades = None, [[] for _ in range(te.stop - te.start)]  # nothing worth trading
        else:
            chosen, test_trades = best_key, runs[best_key][1][te]
        oos.extend(test_trades)
        folds.append({
            "train_period": [str(sessions[tr.start].day), str(sessions[tr.stop - 1].day)],
            "test_period": [str(sessions[te.start].day), str(sessions[te.stop - 1].day)],
            "chosen": chosen,
            "train_sharpe_daily": None if best_score == -math.inf else round(best_score, 4),
            "test": _brief(summarize(test_trades)),
        })
        start += wf.test_sessions

    # Holdout: parameters chosen on the most recent training window before it.
    hold_tr = slice(max(0, n_dev - wf.train_sessions), n_dev)
    best_score, best_key = max((_objective(per[hold_tr]), key) for key, (_, per) in runs.items())
    hold_key = best_key if best_score > 0 and best_score != -math.inf else None
    holdout = summarize(runs[hold_key][1][n_dev:]) if hold_key else summarize([[] for _ in range(n - n_dev)])

    oos_sessions = sessions[n_dev - len(oos):n_dev] if oos else []
    labels = regime_labels(sessions)
    oos_labels = labels[n_dev - len(oos):n_dev] if oos else []
    regimes = {}
    for dim in ("vol", "trend"):
        for name in sorted({lab[dim] for lab in oos_labels}):
            subset = [d for d, lab in zip(oos, oos_labels) if lab[dim] == name]
            regimes[name] = _brief(summarize(subset))

    all_oos = [t for d in oos for t in d]
    return {
        "strategy": cls.name,
        "description": (cls.__doc__ or "").strip().split("\n")[0],
        "configs_tested": len(grid),
        "oos": summarize(oos),
        "oos_period": [str(oos_sessions[0].day), str(oos_sessions[-1].day)] if oos_sessions else None,
        "oos_equity": [round(float(x), 2) for x in np.cumsum(daily_pnl(oos))] if oos else [],
        "oos_days": [str(s.day) for s in oos_sessions],
        "folds": folds,
        "holdout_params": hold_key,
        "holdout": holdout,
        "regimes": regimes,
        "sensitivity": sorted(sensitivity, key=lambda r: -r["net_pnl"]),
        "oos_trades_sample": [t.to_dict() for t in all_oos[-200:]],
    }


def _brief(m: dict) -> dict:
    keys = ("trades", "net_pnl", "profit_factor", "win_rate", "expectancy", "max_drawdown", "sharpe_annual")
    return {k: m[k] for k in keys}


# Promotion gates: a strategy is a *candidate* for paper trading only if all of these hold.
GATES = {
    "min_oos_trades": 100,
    "min_profit_factor": 1.15,
    "min_sharpe_annual": 0.75,
    "expectancy_ci_lower_above_zero": True,
    "positive_fold_share": 0.55,
    "holdout_net_positive": True,
}


def verdict(result: dict, random_baseline: dict | None) -> dict:
    o, h = result["oos"], result["holdout"]
    folds_traded = [f for f in result["folds"] if f["chosen"]]
    pos_share = (sum(1 for f in folds_traded if f["test"]["net_pnl"] > 0) / len(folds_traded)) if folds_traded else 0.0
    checks = {
        "enough_trades": o["trades"] >= GATES["min_oos_trades"],
        "profit_factor": o["profit_factor"] >= GATES["min_profit_factor"],
        "sharpe": o["sharpe_annual"] >= GATES["min_sharpe_annual"],
        "expectancy_significant": (o["expectancy_ci95"][0] or 0) > 0,
        "consistent_folds": pos_share >= GATES["positive_fold_share"],
        "holdout_positive": h["net_pnl"] > 0,
        "beats_random": random_baseline is None or o["sharpe_annual"] > random_baseline["oos"]["sharpe_annual"],
    }
    passed = all(checks.values())
    if passed:
        status, text = "candidate", "Passed every gate. Eligible for paper trading, not yet for live."
    elif o["trades"] < GATES["min_oos_trades"]:
        status, text = "insufficient", "Too few out-of-sample trades to judge."
    else:
        status, text = "rejected", "No reliable edge after costs."
    return {"status": status, "summary": text, "checks": checks, "positive_fold_share": round(pos_share, 3)}
