"""Research job: test every candidate against the baselines and write a reproducible report.

The report is saved under reports/ and registered in the experiments table, along with the
data hash, git commit and full configuration, so any result can be re-run and checked.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..data import Session
from ..fills import CostModel
from ..store import Store
from .engine import BacktestConfig
from .strategies import BASELINES, CANDIDATES, RandomEntry
from .walkforward import GATES, WalkForwardConfig, evaluate_strategy, verdict


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def by_entry_hour(trades: list[dict]) -> list[dict]:
    buckets = defaultdict(list)
    for t in trades:
        buckets[t["entry_time"][:2] + ":00"].append(t["pnl_usd"])
    return [{"hour": h, "trades": len(v), "net_pnl": round(sum(v), 2),
             "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)} for h, v in sorted(buckets.items())]


def by_earnings(trades: list[dict], reaction: dict) -> dict | None:
    if not reaction:
        return None
    out = {}
    for name, keep in (("earnings_reaction_days", True), ("other_days", False)):
        v = [t["pnl_usd"] for t in trades if (reaction.get(datetime.fromisoformat(t["day"]).date(), 0) >= 5) == keep]
        out[name] = {"trades": len(v), "net_pnl": round(sum(v), 2),
                     "expectancy": round(sum(v) / len(v), 2) if v else 0.0}
    return out


def run_research(sessions: list[Session], *, cost: CostModel, data_label: str, data_hash: str,
                 store: Store | None = None, reports_dir: str | Path = "reports",
                 wf: WalkForwardConfig | None = None, earnings_reaction: dict | None = None,
                 progress=print) -> dict:
    wf = wf or WalkForwardConfig()
    need = wf.train_sessions + wf.test_sessions
    if len(sessions) * (1 - wf.holdout_frac) < need:
        raise ValueError(f"Need at least {int(need / (1 - wf.holdout_frac)) + 1} sessions for walk-forward; got "
                         f"{len(sessions)}. Get more history or shrink the windows.")
    bt = BacktestConfig(cost=cost)
    results = {}
    for cls in BASELINES + CANDIDATES:
        progress(f"  evaluating {cls.name} ({len(cls.param_grid())} configs)...")
        results[cls.name] = evaluate_strategy(cls, sessions, bt, wf)

    random_base = results[RandomEntry.name]
    for name, r in results.items():
        r["is_baseline"] = name.startswith("baseline_")
        r["verdict"] = verdict(r, None if r["is_baseline"] else random_base)
        r["by_entry_hour"] = by_entry_hour(r["oos_trades_sample"])
        r["by_earnings"] = by_earnings(r["oos_trades_sample"], earnings_reaction or {})

    candidates = [r for r in results.values() if not r["is_baseline"]]
    passing = [r["strategy"] for r in candidates if r["verdict"]["status"] == "candidate"]
    exp_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report = {
        "id": exp_id,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "data": {"label": data_label, "sha256": data_hash, "sessions": len(sessions),
                 "first": str(sessions[0].day), "last": str(sessions[-1].day),
                 "synthetic": data_label.startswith("synthetic")},
        "costs": {"instrument": cost.instrument.root, "point_value": cost.instrument.point_value,
                  "commission_rt": cost.commission, "slippage_ticks_per_market_or_stop_fill": cost.slippage_ticks,
                  "limit_fill_rule": "requires trade-through by 1 tick"},
        "walk_forward": asdict(wf),
        "gates": GATES,
        "total_configs_tested": sum(r["configs_tested"] for r in results.values()),
        "results": results,
        "conclusion": (f"{len(passing)} strategy(ies) passed every gate: {', '.join(passing)}. They are eligible "
                       "for paper trading. That is not proof of a live edge." if passing else
                       "No candidate showed a reliable edge after costs. The system should not trade any of them live."),
    }
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"research_{exp_id}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    (out_dir / f"research_{exp_id}.md").write_text(to_markdown(report))
    if store:
        store.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?)",
                      (exp_id, report["created"], "research", report["git_sha"], data_hash, data_label,
                       json.dumps({"walk_forward": asdict(wf), "costs": report["costs"], "gates": GATES}),
                       json.dumps({n: {"verdict": r["verdict"]["status"], "oos": r["oos"],
                                       "holdout_params": r["holdout_params"]} for n, r in results.items()}),
                       str(path)))
        store.set("latest_research", str(path))
    return report


def to_markdown(rep: dict) -> str:
    L = [f"# Research report {rep['id']}", "",
         f"- Data: **{rep['data']['label']}**, {rep['data']['sessions']} sessions "
         f"({rep['data']['first']} to {rep['data']['last']}), sha256 `{rep['data']['sha256'][:12]}`",
         f"- Code: `{rep['git_sha']}`. Configurations tested: {rep['total_configs_tested']}",
         f"- Costs: {rep['costs']}",
         "", "**All figures are simulated. None of this is live performance.**", "",
         f"## Conclusion\n\n{rep['conclusion']}", "",
         "## Out-of-sample (walk-forward) results", "",
         "| Strategy | Verdict | Trades | Net P&L | PF | Win % | Expectancy (95% CI) | Max DD | Sharpe | Holdout P&L |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in rep["results"].items():
        o, h = r["oos"], r["holdout"]
        ci = o["expectancy_ci95"]
        L.append(f"| {name} | {r['verdict']['status']} | {o['trades']} | {o['net_pnl']:,.0f} | {o['profit_factor']} | "
                 f"{o['win_rate']:.0%} | {o['expectancy']:.1f} ({ci[0]}, {ci[1]}) | {o['max_drawdown']:,.0f} | "
                 f"{o['sharpe_annual']} | {h['net_pnl']:,.0f} |")
    for name, r in rep["results"].items():
        if r["is_baseline"]:
            continue
        L += ["", f"### {name}", "", r["description"], "",
              f"Verdict: **{r['verdict']['status']}**. {r['verdict']['summary']} Checks: {r['verdict']['checks']}", "",
              "Regimes (out-of-sample, labelled after the fact): " +
              ", ".join(f"{k}: {v['trades']} trades, ${v['net_pnl']:,.0f}" for k, v in r["regimes"].items()), "",
              "Sensitivity (development period, in-sample):", "", "| Params | Trades | Net | PF | Sharpe |", "|---|---|---|---|---|"]
        for srow in r["sensitivity"]:
            L.append(f"| {srow['key']} | {srow['trades']} | {srow['net_pnl']:,.0f} | {srow['profit_factor']} | "
                     f"{srow['sharpe_annual']} |")
    return "\n".join(L) + "\n"
