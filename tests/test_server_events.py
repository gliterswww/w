from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from nqtrader import events as ev
from nqtrader import versions
from nqtrader.data import ET
from nqtrader.server import create_app, market_status
from nqtrader.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("NQT_DASHBOARD_TOKEN", raising=False)
    store = Store(tmp_path / "s.db")
    return TestClient(create_app(store)), store


def test_state_and_pages(client):
    c, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    s = c.get("/api/state").json()
    assert "market" in s and s["kill_switch"] is False
    for path in ("/api/health", "/api/bars", "/api/trades", "/api/performance", "/api/events", "/api/research",
                 "/api/versions", "/api/news", "/api/alerts", "/api/preflight"):
        assert c.get(path).status_code == 200, path


def test_kill_and_resume_requires_confirmation(client):
    c, store = client
    assert c.post("/api/control/kill", json={"reason": "test"}).json()["kill_switch"] is True
    assert store.get("kill_switch") is True
    assert c.post("/api/control/resume", json={"confirm": "yes"}).status_code == 400
    assert c.post("/api/control/resume", json={"confirm": "RESUME"}).json()["kill_switch"] is False


def test_alert_validation(client):
    c, _ = client
    assert c.post("/api/alerts", json={"price": -5, "direction": "above"}).status_code == 422
    assert c.post("/api/alerts", json={"price": 20000, "direction": "sideways"}).status_code == 422
    aid = c.post("/api/alerts", json={"price": 20000.1, "direction": "above", "note": "x"}).json()["id"]
    assert c.get("/api/alerts").json()[0]["price"] == 20000.0  # snapped to tick
    c.delete(f"/api/alerts/{aid}")
    assert c.get("/api/alerts").json() == []


def test_token_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NQT_DASHBOARD_TOKEN", "s3cret")
    c = TestClient(create_app(Store(tmp_path / "a.db")))
    assert c.post("/api/control/kill", json={}).status_code == 401
    assert c.post("/api/control/kill", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/control/kill", json={}, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/state").status_code == 200  # reads stay open


def test_dashboard_write_surface_is_minimal(client):
    """The dashboard can't enable live trading, edit risk limits, or place orders."""
    c, _ = client
    writes = {(r.path, m) for r in c.app.routes for m in getattr(r, "methods", set()) if m in ("POST", "PUT", "PATCH", "DELETE")}
    assert writes == {("/api/control/kill", "POST"), ("/api/control/resume", "POST"), ("/api/alerts", "POST"),
                      ("/api/alerts/{alert_id}", "DELETE"), ("/api/news/refresh", "POST"),
                      ("/api/webhook/tradingview", "POST")}


def test_tradingview_webhook(tmp_path, monkeypatch):
    store = Store(tmp_path / "tv.db")
    c = TestClient(create_app(store))
    body = {"secret": "abc", "kind": "orb_long", "ticker": "NQ1!", "price": 21000.5, "time": "2026-10-26T14:05:00Z"}
    monkeypatch.delenv("NQT_TV_WEBHOOK_SECRET", raising=False)
    assert c.post("/api/webhook/tradingview", json=body).status_code == 404       # disabled by default
    monkeypatch.setenv("NQT_TV_WEBHOOK_SECRET", "abc")
    c = TestClient(create_app(store))
    assert c.post("/api/webhook/tradingview", json={**body, "secret": "x"}).status_code == 401
    assert c.post("/api/webhook/tradingview", json={**body, "kind": "DROP TABLE"}).status_code == 422
    assert c.post("/api/webhook/tradingview", json=body).json()["status"] == "logged"
    assert c.post("/api/webhook/tradingview", json=body).json()["status"] == "duplicate ignored"
    assert store.query("SELECT COUNT(*) n FROM orders")[0]["n"] == 0
    assert "TradingView: orb long" in store.events(1)[0]["message"]


def test_market_status():
    assert market_status(datetime(2026, 10, 26, 10, 0, tzinfo=ET))["state"] == "rth"
    assert market_status(datetime(2026, 10, 26, 17, 30, tzinfo=ET))["state"] == "break"
    assert market_status(datetime(2026, 10, 24, 12, 0, tzinfo=ET))["state"] == "closed"
    assert market_status(datetime(2026, 10, 25, 19, 0, tzinfo=ET))["state"] == "globex"


def test_calendar_blackout(tmp_path):
    p = tmp_path / "cal.csv"
    p.write_text("datetime_et,event,impact,source\n2026-10-28 14:00,FOMC,high,x\n2026-10-28 10:00,Minor,low,x\nbad,row,high,x\n")
    cal = ev.load_calendar(p)
    assert len(cal) == 2
    at = lambda h, m: datetime(2026, 10, 28, h, m, tzinfo=ET)  # noqa: E731
    assert ev.blackout_reason(cal, at(13, 56), 5, 10)
    assert ev.blackout_reason(cal, at(14, 10), 5, 10)
    assert ev.blackout_reason(cal, at(14, 11), 5, 10) is None
    assert ev.blackout_reason(cal, at(10, 0), 5, 10) is None   # low impact ignored


def test_earnings_reaction_days():
    assert ev.reaction_day(date(2026, 10, 29), "amc") == date(2026, 10, 30)
    assert ev.reaction_day(date(2026, 10, 30), "amc") == date(2026, 11, 2)   # Friday -> Monday
    assert ev.reaction_day(date(2026, 10, 29), "bmo") == date(2026, 10, 29)
    out = ev.earnings_outlook([{"symbol": "NVDA", "date": "2026-10-29", "timing": "amc", "source": "config"}],
                              date(2026, 10, 30))
    assert out["today_flag"] == "elevated"


def test_headline_tagging():
    assert "Fed & rates" in ev.tag_headline("Powell signals rate cut as Fed meets")
    assert ev.tag_headline("Local bakery wins award") == []


def test_promotion_requires_validation(tmp_path):
    store = Store(tmp_path / "v.db")
    assert versions.production(store)["status"] == "paper-only"
    with pytest.raises(ValueError):
        versions.promote(store, "orb", {"minutes": 30, "target_r": 2.0}, None)
    import json
    store.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?)",
                  ("e1", "now", "research", "sha", "h", "real.csv", "{}",
                   json.dumps({"orb": {"verdict": "rejected", "holdout_params": "orb(minutes=30,target_r=2.0)"}}), "p"))
    with pytest.raises(ValueError, match="did not pass"):
        versions.promote(store, "orb", {"minutes": 30, "target_r": 2.0}, "e1")
    store.execute("UPDATE experiments SET summary=? WHERE id='e1'",
                  (json.dumps({"orb": {"verdict": "candidate", "holdout_params": "orb(minutes=30,target_r=2.0)"}}),))
    with pytest.raises(ValueError, match="validated parameters"):
        versions.promote(store, "orb", {"minutes": 15, "target_r": 2.0}, "e1")
    v = versions.promote(store, "orb", {"minutes": 30, "target_r": 2.0}, "e1")
    assert v["status"] == "production"
    v = versions.rollback(store)
    assert v["version"] == 1
