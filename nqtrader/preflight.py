"""Pre-live checklist. Every item must pass before the first live order can be authorized.

Passing this checklist only makes going live *possible*. A human still has to authorize it,
and the authorization is tied to the exact strategy version and risk-limit digest.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import events as ev
from . import versions
from .data import ET
from .risk import load_limits
from .runner import CONFIG
from .store import Store

MIN_PAPER_TRADES = 30
MIN_PAPER_SESSIONS = 20


def live_checklist(store: Store) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("Live broker adapter", False, "Not built yet. Needs a funded Tradovate account with API access "
                                      "(see docs/ARCHITECTURE.md, Phase 4).")
    v = versions.production(store)
    add("Validated strategy version", v["status"] == "production",
        f"v{v['version']} {v['strategy']} is '{v['status']}'" + ("" if v["status"] == "production" else
                                                                 ": promote a version that passed validation"))
    exp = store.query("SELECT data_label FROM experiments WHERE id=?", (v.get("experiment_id") or "",))
    real_data = bool(exp) and not exp[0]["data_label"].startswith("synthetic")
    add("Validated on real market data", real_data,
        exp[0]["data_label"] if exp else "no validating experiment")
    paper = store.query("SELECT COUNT(*) n, COUNT(DISTINCT day) d, COALESCE(SUM(CASE WHEN pnl_usd>0 THEN pnl_usd END),0) w, "
                        "COALESCE(-SUM(CASE WHEN pnl_usd<=0 THEN pnl_usd END),0) l FROM trades WHERE mode='paper'")[0]
    synthetic_paper = str(store.get("data_label", "")).startswith("synthetic")
    pf = paper["w"] / paper["l"] if paper["l"] else 0
    add("Paper track record", paper["n"] >= MIN_PAPER_TRADES and paper["d"] >= MIN_PAPER_SESSIONS
        and pf >= 1.0 and not synthetic_paper,
        f"{paper['n']} trades over {paper['d']} sessions, PF {pf:.2f}"
        + (" (synthetic replay doesn't count)" if synthetic_paper else "")
        + f"; need >= {MIN_PAPER_TRADES} trades, >= {MIN_PAPER_SESSIONS} sessions, PF >= 1.0 on live data")
    try:
        lim = load_limits(CONFIG / "risk.json")
        add("Risk limits valid", True, f"digest {lim.digest()}")
        add("Conservative first-live sizing", lim.risk_per_trade_usd <= 0.01 * lim.account_capital,
            f"risk/trade ${lim.risk_per_trade_usd:.0f} vs 1% of capital ${0.01 * lim.account_capital:.0f}")
    except Exception as exc:
        add("Risk limits valid", False, str(exc))
    cal = ev.load_calendar(CONFIG / "economic_calendar.csv")
    now = datetime.now(ET)
    soon = [e for e in cal if now <= e.at <= now + timedelta(days=30)]
    add("Economic calendar current", len(soon) >= 3,
        f"{len(soon)} events in the next 30 days (need the full FOMC/CPI/NFP/PPI schedule)")
    add("Dashboard token set", bool(os.environ.get("NQT_DASHBOARD_TOKEN")), "NQT_DASHBOARD_TOKEN env var")
    add("Broker credentials in environment", all(os.environ.get(k) for k in
                                                   ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_APP_ID",
                                                    "TRADOVATE_CID", "TRADOVATE_SECRET")),
        "TRADOVATE_* env vars (never in files or the repo)")
    add("Emergency stop released", not store.get("kill_switch", False), "kill switch state")
    crit = store.query("SELECT COUNT(*) n FROM events WHERE level='critical' AND ts >= ?",
                       ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))[0]["n"]
    add("No critical events in last 24h", crit == 0, f"{crit} critical events")
    return checks
