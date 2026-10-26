"""Command line: python -m nqtrader <command>

  research   Walk-forward test every strategy vs baselines (--data CSV, or --synthetic)
  demo       Synthetic research + a 30-session paper replay, to try the dashboard
  paper      Paper-trade today's session on delayed Yahoo data
  serve      Start the dashboard (http://127.0.0.1:8000)
  refresh    Fetch news headlines and mega-cap earnings dates
  versions   List strategy versions
  promote    Promote a validated strategy (or --paper-only)
  rollback   Revert to the previous strategy version
  kill       Engage the emergency stop      resume   Release it
  authorize-live   Pre-live checklist (live trading is not available yet)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .data import file_sha256, frame_sha256, load_bars_csv, split_sessions
from .fills import CostModel
from .instruments import INSTRUMENTS
from .runner import CONFIG, ROOT, refresh_external, run_live_paper, run_replay
from .store import Store

DB = Path(os.environ.get("NQT_DB", ROOT / "var" / "nqtrader.db"))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="nqtrader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", help="CSV of 1-minute NQ bars (Databento, TradingView or generic)")
    src.add_argument("--synthetic", action="store_true", help="random-walk test data (pipeline check)")
    r.add_argument("--sessions", type=int, default=600)
    r.add_argument("--edge", choices=["orb"], help="inject a known edge into synthetic data")
    r.add_argument("--instrument", default="NQ", choices=list(INSTRUMENTS))
    r.add_argument("--slippage-ticks", type=float, default=1.0)
    r.add_argument("--commission", type=float, help="round trip per contract (default: instrument estimate)")
    r.add_argument("--train", type=int, default=250)
    r.add_argument("--test", type=int, default=63)

    d = sub.add_parser("demo")
    d.add_argument("--sessions", type=int, default=520)
    d.add_argument("--reset", action="store_true", help="delete the existing demo database first")

    p = sub.add_parser("paper")
    p.add_argument("--limits", default=str(CONFIG / "risk.paper.json"))

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    sub.add_parser("refresh")
    sub.add_parser("versions")
    pr = sub.add_parser("promote")
    pr.add_argument("strategy")
    pr.add_argument("--params", default="{}", help='JSON, e.g. \'{"minutes": 30, "target_r": 2.0}\'')
    pr.add_argument("--experiment")
    pr.add_argument("--paper-only", action="store_true")
    pr.add_argument("--notes", default="")
    sub.add_parser("rollback")
    k = sub.add_parser("kill")
    k.add_argument("--reason", default="manual (CLI)")
    sub.add_parser("resume")
    sub.add_parser("authorize-live")

    a = ap.parse_args(argv)
    store = Store(DB)

    if a.cmd == "research":
        from .research.run import run_research
        from .research.synthetic import generate_bars
        from .research.walkforward import WalkForwardConfig
        if a.synthetic:
            df = generate_bars(a.sessions, edge=a.edge)
            label, digest = f"synthetic-{a.edge or 'randomwalk'}", frame_sha256(df)
        else:
            df, label, digest = load_bars_csv(a.data), Path(a.data).name, file_sha256(a.data)
        sessions = split_sessions(df)
        print(f"{len(sessions)} sessions from {label}")
        cost = CostModel(INSTRUMENTS[a.instrument], a.slippage_ticks, a.commission)
        rep = run_research(sessions, cost=cost, data_label=label, data_hash=digest, store=store,
                           reports_dir=ROOT / "reports", wf=WalkForwardConfig(a.train, a.test))
        print(f"\n{rep['conclusion']}\nReport: reports/research_{rep['id']}.md")

    elif a.cmd == "demo":
        from .research.run import run_research
        from .research.synthetic import generate_bars
        from .research.walkforward import WalkForwardConfig
        if a.reset and DB.exists():
            for suffix in ("", "-wal", "-shm"):
                Path(str(DB) + suffix).unlink(missing_ok=True)
            store = Store(DB)
        import pandas as pd
        end = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None) - pd.tseries.offsets.BDay(1)
        df = generate_bars(a.sessions, seed=11, start=str(pd.bdate_range(end=end, periods=a.sessions)[0].date()),
                           start_price=24000.0)
        label = "synthetic-demo"
        print("Research on synthetic data...")
        run_research(split_sessions(df), cost=CostModel(INSTRUMENTS["NQ"]), data_label=label,
                     data_hash=frame_sha256(df), store=store, reports_dir=ROOT / "reports",
                     wf=WalkForwardConfig(250, 63))
        print("Paper replay of the last 30 sessions...")
        _seed_demo_alerts(store, df)
        run_replay(df, store, trade_sessions=30, data_label=label, limits_path=CONFIG / "risk.demo.json")
        trades = store.query("SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) p FROM trades")[0]
        print(f"Done: {trades['n']} simulated trades, P&L ${trades['p']:+,.2f}. Now run: python -m nqtrader serve")

    elif a.cmd == "paper":
        run_live_paper(store, limits_path=Path(a.limits))

    elif a.cmd == "serve":
        import uvicorn
        from .server import create_app
        if a.host not in ("127.0.0.1", "localhost") and not os.environ.get("NQT_DASHBOARD_TOKEN"):
            sys.exit("Refusing to listen on a public interface without NQT_DASHBOARD_TOKEN set.")
        uvicorn.run(create_app(store), host=a.host, port=a.port, log_level="info")

    elif a.cmd == "refresh":
        print(json.dumps(refresh_external(store), indent=2))

    elif a.cmd in ("versions", "promote", "rollback"):
        from . import versions
        if a.cmd == "promote":
            versions.promote(store, a.strategy, json.loads(a.params), a.experiment, a.paper_only, a.notes)
        elif a.cmd == "rollback":
            versions.rollback(store)
        for v in versions.history(store):
            print(f"v{v['version']:<3} {v['status']:<12} {v['strategy']} {v['params']}  exp={v['experiment_id']}")

    elif a.cmd == "kill":
        store.set("kill_switch", True)
        store.log("critical", "control", f"EMERGENCY STOP engaged: {a.reason}")
        print("Emergency stop engaged. The engine flattens and stops opening positions on its next bar.")

    elif a.cmd == "resume":
        store.set("kill_switch", False)
        store.log("warning", "control", "Emergency stop released (CLI)")
        print("Emergency stop released.")

    elif a.cmd == "authorize-live":
        from .preflight import live_checklist
        checks = live_checklist(store)
        for c in checks:
            print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
        sys.exit(0 if all(c["ok"] for c in checks) else 1)


def _seed_demo_alerts(store: Store, df) -> None:
    last = float(df["open"][df.index.date == df.index[-1].date()].iloc[0])
    if not store.query("SELECT 1 FROM alerts"):
        for price, direction, note in ((last * 1.004, "above", "demo: breakout watch"),
                                       (last * 0.996, "below", "demo: support")):
            store.execute("INSERT INTO alerts(created,price,direction,note) VALUES(datetime('now'),?,?,?)",
                          (round(price * 4) / 4, direction, note))


if __name__ == "__main__":
    main()
