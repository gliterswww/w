"""Dashboard server: read-only views of engine state, plus a few authenticated controls.

The dashboard can engage or release the emergency stop and manage price alerts. It cannot change
risk limits, switch to live trading, or place orders; those need the CLI and the config files.
Broker credentials never reach this process.
"""
from __future__ import annotations

import json
import math
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, versions
from .data import ET
from .store import Store

STATIC = Path(__file__).parent / "dashboard"


class AlertIn(BaseModel):
    price: float = Field(gt=0, lt=1_000_000)
    direction: str = Field(pattern="^(above|below)$")
    note: str = Field("", max_length=120)


class KillIn(BaseModel):
    reason: str = Field("dashboard", max_length=200)


class ResumeIn(BaseModel):
    confirm: str


class TradingViewIn(BaseModel):
    model_config = {"extra": "ignore"}
    secret: str = Field(max_length=200)
    kind: str = Field(max_length=40, pattern=r"^[a-z0-9_]+$")
    ticker: str = Field("", max_length=40)
    price: float | None = None
    time: str = Field("", max_length=40)


def market_status(now: datetime) -> dict:
    """CME Globex equity futures hours (ET). Exchange holidays aren't modelled here."""
    wd, t = now.weekday(), now.hour * 60 + now.minute
    if wd == 5 or (wd == 6 and t < 18 * 60) or (wd == 4 and t >= 17 * 60):
        return {"state": "closed", "label": "Closed (weekend)"}
    if 17 * 60 <= t < 18 * 60:
        return {"state": "break", "label": "Daily maintenance break"}
    if 9 * 60 + 30 <= t < 16 * 60 and wd < 5:
        return {"state": "rth", "label": "Regular session open"}
    return {"state": "globex", "label": "Overnight (Globex) session"}


def _clean(o):
    """JSON can't carry NaN/Infinity; send null instead."""
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="nqtrader", version=__version__, docs_url=None, redoc_url=None)
    token = os.environ.get("NQT_DASHBOARD_TOKEN")

    def auth(authorization: str | None = Header(None)):
        if token and not (authorization and secrets.compare_digest(authorization, f"Bearer {token}")):
            raise HTTPException(401, "missing or invalid dashboard token")

    def ok(data):
        return JSONResponse(_clean(data))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/health")
    def health():
        hb = store.get("engine_heartbeat")
        state = store.get("engine_state") or {}
        return ok({"status": "ok", "version": __version__, "db": store.path, "heartbeat": hb,
                   "engine_as_of": state.get("as_of"), "auth_required": bool(token)})

    @app.get("/api/state")
    def state():
        now = datetime.now(ET)
        s = store.get("engine_state") or {}
        return ok({**s, "server_time": now.isoformat(timespec="seconds"), "market": market_status(now),
                   "kill_switch": bool(store.get("kill_switch", False)),
                   "heartbeat": store.get("engine_heartbeat"), "scanner": store.get("scanner"),
                   "data_label": s.get("data_label") or store.get("data_label"),
                   "auth_required": bool(token)})

    @app.get("/api/bars")
    def bars(day: str | None = None):
        if day is None:
            last = store.query("SELECT MAX(substr(ts,1,10)) d FROM bars")[0]["d"]
            day = last
        rows = store.query("SELECT * FROM bars WHERE substr(ts,1,10)=? ORDER BY ts", (day or "",))
        days = [r["d"] for r in store.query("SELECT DISTINCT substr(ts,1,10) d FROM bars ORDER BY d DESC LIMIT 30")]
        return ok({"day": day, "bars": rows, "days": days,
                   "trades": store.query("SELECT * FROM trades WHERE day=? ORDER BY id", (day or "",))})

    @app.get("/api/trades")
    def trades(limit: int = 500):
        return ok(store.query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (min(limit, 5000),)))

    @app.get("/api/performance")
    def performance():
        rows = store.query("SELECT * FROM trades ORDER BY id")
        return ok(_performance(rows, store))

    @app.get("/api/events")
    def events(limit: int = 200, level: str | None = None):
        if level:
            return ok(store.query("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, min(limit, 2000))))
        return ok(store.events(min(limit, 2000)))

    @app.get("/api/research")
    def research(exp_id: str | None = None):
        if exp_id:
            rows = store.query("SELECT report_path FROM experiments WHERE id=?", (exp_id,))
            path = rows[0]["report_path"] if rows else None
        else:
            path = store.get("latest_research")
        if not path or not Path(path).exists():
            return ok({"available": False})
        rep = json.loads(Path(path).read_text())
        for r in rep["results"].values():
            r.pop("oos_trades_sample", None)
        experiments = store.query("SELECT id, created, data_label, git_sha FROM experiments ORDER BY created DESC LIMIT 20")
        return ok({"available": True, "report": rep, "experiments": experiments})

    @app.get("/api/versions")
    def get_versions():
        return ok({"current": versions.production(store), "history": versions.history(store)})

    @app.get("/api/news")
    def news():
        s = store.get("engine_state") or {}
        return ok({"news": store.get("news_cache") or {"items": [], "error": "not fetched yet: run python -m nqtrader refresh"},
                   "econ": s.get("econ_upcoming", []), "earnings": s.get("earnings"),
                   "earnings_fetch": store.get("earnings_fetch"), "blackout": s.get("news_blackout")})

    @app.post("/api/news/refresh", dependencies=[Depends(auth)])
    def news_refresh():
        from .runner import refresh_external
        return ok(refresh_external(store))

    @app.get("/api/alerts")
    def alerts():
        return ok(store.query("SELECT * FROM alerts ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT 200"))

    @app.post("/api/alerts", dependencies=[Depends(auth)])
    def add_alert(a: AlertIn):
        if len(store.query("SELECT 1 FROM alerts WHERE status='active'")) >= 50:
            raise HTTPException(400, "too many active alerts (max 50)")
        aid = store.execute("INSERT INTO alerts(created,price,direction,note) VALUES(?,?,?,?)",
                            (datetime.now(ET).isoformat(timespec="seconds"), round(a.price * 4) / 4, a.direction, a.note))
        store.log("info", "alerts", f"Alert created: {a.direction} {a.price:.2f}")
        return ok({"id": aid})

    @app.delete("/api/alerts/{alert_id}", dependencies=[Depends(auth)])
    def delete_alert(alert_id: int):
        store.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        return ok({"deleted": alert_id})

    @app.post("/api/control/kill", dependencies=[Depends(auth)])
    def kill(k: KillIn):
        store.set("kill_switch", True)
        store.log("critical", "control", f"EMERGENCY STOP engaged from dashboard: {k.reason}")
        return ok({"kill_switch": True})

    @app.post("/api/control/resume", dependencies=[Depends(auth)])
    def resume(r: ResumeIn):
        if r.confirm != "RESUME":
            raise HTTPException(400, 'type RESUME to confirm')
        store.set("kill_switch", False)
        store.log("warning", "control", "Emergency stop released from dashboard")
        return ok({"kill_switch": False})

    tv_seen: dict[str, float] = {}

    @app.post("/api/webhook/tradingview")
    def tradingview(a: TradingViewIn):
        """TradingView alert -> dashboard notification. Never an order.

        TradingView can't send auth headers, so a shared secret travels in the JSON body.
        Disabled unless NQT_TV_WEBHOOK_SECRET is set.
        """
        tv_secret = os.environ.get("NQT_TV_WEBHOOK_SECRET")
        if not tv_secret:
            raise HTTPException(404, "TradingView webhook disabled")
        if not secrets.compare_digest(a.secret, tv_secret):
            raise HTTPException(401, "bad secret")
        key = f"{a.kind}|{a.ticker}|{a.time[:16]}"
        now = datetime.now().timestamp()
        for k in [k for k, t in tv_seen.items() if now - t > 600]:
            del tv_seen[k]
        if key in tv_seen:
            return ok({"status": "duplicate ignored"})
        if len(tv_seen) > 500:
            raise HTTPException(429, "too many alerts")
        tv_seen[key] = now
        price = f" @ {a.price:.2f}" if a.price is not None else ""
        store.log("alert", "tradingview", f"TradingView: {a.kind.replace('_', ' ')} {a.ticker}{price}",
                  kind=a.kind, ticker=a.ticker, price=a.price, tv_time=a.time)
        return ok({"status": "logged"})

    @app.get("/api/preflight")
    def preflight():
        from .preflight import live_checklist
        return ok(live_checklist(store))

    return app


def _performance(rows: list[dict], store: Store) -> dict:
    pnl = [r["pnl_usd"] for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    by_day: dict[str, float] = {}
    for r in rows:
        by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["pnl_usd"]
    for day in sorted(by_day):
        eq += by_day[day]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        curve.append({"time": day, "value": round(eq, 2), "day_pnl": round(by_day[day], 2)})
    hours: dict[str, list] = {}
    strategies: dict[str, list] = {}
    for r in rows:
        hours.setdefault((r["entry_time"] or "")[11:13] + ":00", []).append(r["pnl_usd"])
        strategies.setdefault(r["strategy"], []).append(r["pnl_usd"])

    def grp(d):
        return [{"key": k, "trades": len(v), "net_pnl": round(sum(v), 2),
                 "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)} for k, v in sorted(d.items())]

    capital = (store.get("engine_state") or {}).get("budgets", {}).get("capital")
    return {
        "trades": len(rows), "net_pnl": round(sum(pnl), 2),
        "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "expectancy": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "avg_r": round(sum(r["r_multiple"] or 0 for r in rows) / len(rows), 3) if rows else None,
        "max_drawdown": round(dd, 2), "best_day": max(by_day.values(), default=None),
        "worst_day": min(by_day.values(), default=None), "sessions_traded": len(by_day),
        "equity_curve": curve, "by_hour": grp(hours), "by_strategy": grp(strategies), "capital": capital,
        "mode": store.get("mode"), "data_label": store.get("data_label"),
    }
