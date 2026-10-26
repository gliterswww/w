"""Live engine: parity with the backtester, recovery paths, idempotency and controls."""
import pytest

from nqtrader import events as ev
from nqtrader.brokers import PaperBroker
from nqtrader.engine import Engine
from nqtrader.feeds import ReplayFeed
from nqtrader.fills import CostModel
from nqtrader.instruments import MNQ
from nqtrader.research.engine import BacktestConfig, run_session
from nqtrader.research.strategies import ALL
from nqtrader.risk import RiskLimits
from nqtrader.store import Store

LOOSE = dict(account_capital=1_000_000, risk_per_trade_usd=20_000, max_daily_loss_usd=1e6, max_weekly_loss_usd=1e6,
             max_monthly_loss_usd=1e6, max_total_drawdown_usd=1e6, max_contracts=1, max_order_size=1,
             max_trades_per_day=50, max_consecutive_losses=20, max_slippage_ticks=1000, no_entries_after="15:54")


def make_engine(tmp_path, sessions, strategy="orb", params=None, limits=None, calendar=None):
    store = Store(tmp_path / "t.db")
    cost = CostModel(MNQ)
    broker = PaperBroker(cost, 1_000_000)
    eng = Engine(store=store, broker=broker, instrument=MNQ, limits=limits or RiskLimits(**LOOSE),
                 strategy_name=strategy, strategy_params=params or {"minutes": 30, "target_r": 2.0},
                 strategy_version=1, history=sessions, calendar=calendar or [], earnings=[], cost=cost)
    return eng, store, broker


def feed_days(eng, df, days):
    for bar in ReplayFeed(df[df.index.normalize().isin(days)]):
        eng.on_bar(bar)


@pytest.mark.parametrize("strategy,params", [("orb", {"minutes": 30, "target_r": 2.0}),
                                             ("orb", {"minutes": 15, "target_r": 0}),
                                             ("vwap_reversion", {"k": 2.0, "stop_atr": 0.1}),
                                             ("failed_breakout", {"levels": "prior_day", "target_r": 1.0, "reclaim_bars": 10}),
                                             ("intraday_momentum", {"window": "day", "min_move_atr": 0.0})])
def test_engine_matches_backtest(tmp_path, random_walk_bars, random_walk_sessions, strategy, params):
    """The live engine + paper broker must reproduce the backtester trade-for-trade."""
    hist, live = random_walk_sessions[:60], random_walk_sessions[60:80]
    eng, store, _ = make_engine(tmp_path, hist, strategy, params)
    days = [d for d in random_walk_bars.index.normalize().unique() if d.date() in {s.day for s in live}]
    feed_days(eng, random_walk_bars, days)
    got = [(t["day"], t["side"], t["entry"], t["exit"], t["reason"])
           for t in store.query("SELECT * FROM trades ORDER BY id")]
    cfg = BacktestConfig(cost=CostModel(MNQ))
    want = []
    for s in live:
        for t in run_session(ALL[strategy](**params), s, cfg):
            want.append((str(t.day), t.side, t.entry, t.exit, {"time": "flatten", "eod": "flatten"}.get(t.reason, t.reason)))
    assert len(want) > 0
    assert got == want


def _run_until_position(eng, df, sessions):
    for bar in ReplayFeed(df[df.index.date == sessions[0].day]):
        eng.on_bar(bar)
        if eng.position:
            return bar
    pytest.skip("no entry on this synthetic day")


def test_missing_stop_is_restored(tmp_path, random_walk_bars, random_walk_sessions):
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    broker.drop_stop_next = True
    days = random_walk_sessions[60:70]
    for s in days:
        for bar in ReplayFeed(random_walk_bars[random_walk_bars.index.date == s.day]):
            eng.on_bar(bar)
            if eng.position:
                assert any(o.kind == "stop" for o in broker.working_orders()), "stop not restored"
                msgs = [e["message"] for e in store.events(50)]
                assert any("re-placing" in m for m in msgs) and any("restored" in m for m in msgs)
                assert eng.risk.lockout  # and no new entries today
                return
    pytest.fail("never entered a position")


def test_unknown_position_triggers_lockout(tmp_path, random_walk_bars, random_walk_sessions):
    from nqtrader.brokers.base import BrokerPosition
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    day = random_walk_sessions[60].day
    bars = ReplayFeed(random_walk_bars[random_walk_bars.index.date == day]).bars
    eng.on_bar(bars[0])
    broker._position = BrokerPosition(eng.contract.symbol, 2, bars[0].close)  # someone traded manually
    eng.on_bar(bars[1])
    assert eng.risk.lockout and eng.position and eng.position.qty == 2
    assert any(o.kind == "stop" for o in broker.working_orders()) or eng.flatten_requested
    assert any(e["level"] == "critical" for e in store.events(20))


def test_disconnect_blocks_entries(tmp_path, random_walk_bars, random_walk_sessions):
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    broker.connected_flag = False
    feed_days(eng, random_walk_bars, [random_walk_bars.index.normalize().unique()[60]])
    assert store.query("SELECT COUNT(*) n FROM orders")[0]["n"] == 0


def test_rejected_order_is_logged_not_retried(tmp_path, random_walk_bars, random_walk_sessions):
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    broker.reject_next = "insufficient margin"
    feed_days(eng, random_walk_bars, list(random_walk_bars.index.normalize().unique()[60:62]))
    statuses = [o["status"] for o in store.query("SELECT status FROM orders ORDER BY ts")]
    assert statuses and statuses[0] == "rejected"


def test_kill_switch_flattens_once_and_blocks(tmp_path, random_walk_bars, random_walk_sessions):
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    for s in random_walk_sessions[60:75]:
        for bar in ReplayFeed(random_walk_bars[random_walk_bars.index.date == s.day]):
            eng.on_bar(bar)
            if eng.position and not store.get("kill_switch"):
                store.set("kill_switch", True)
    flattens = [e for e in store.events(2000) if e["message"].startswith("FLATTEN: emergency")]
    assert len(flattens) == 1
    trades = store.query("SELECT reason FROM trades")
    assert trades[-1]["reason"] == "flatten"
    assert eng.position is None
    assert store.query("SELECT COUNT(*) n FROM orders")[0]["n"] == 1   # nothing after the stop


def test_duplicate_client_id_ignored(tmp_path, random_walk_sessions):
    eng, store, broker = make_engine(tmp_path, random_walk_sessions[:60])
    a1 = broker.place_bracket(client_id="x", symbol="MNQZ6", side=1, qty=1, ref_price=1, stop=0.5, target=2)
    a2 = broker.place_bracket(client_id="x", symbol="MNQZ6", side=1, qty=1, ref_price=1, stop=0.5, target=2)
    assert a1.accepted and a2.reason == "duplicate ignored" and len(broker._brackets) == 1


def test_news_blackout_blocks(tmp_path, random_walk_bars, random_walk_sessions):
    day = random_walk_sessions[60].day
    from datetime import datetime
    from nqtrader.data import ET
    cal = [ev.EconEvent(datetime.combine(day, datetime.min.time()).replace(hour=h, tzinfo=ET), "test", "high")
           for h in range(10, 16)]
    limits = RiskLimits(**{**LOOSE, "news_blackout_minutes_before": 60, "news_blackout_minutes_after": 60})
    eng, store, _ = make_engine(tmp_path, random_walk_sessions[:60], limits=limits, calendar=cal)
    feed_days(eng, random_walk_bars, [random_walk_bars.index.normalize().unique()[60]])
    assert store.query("SELECT COUNT(*) n FROM orders")[0]["n"] == 0


def test_price_alerts(tmp_path, random_walk_bars, random_walk_sessions):
    eng, store, _ = make_engine(tmp_path, random_walk_sessions[:60])
    day_df = random_walk_bars[random_walk_bars.index.date == random_walk_sessions[60].day]
    hi = float(day_df["high"].max())
    store.execute("INSERT INTO alerts(created,price,direction,note) VALUES('x',?,?,?)", (hi - 0.25, "above", "a"))
    store.execute("INSERT INTO alerts(created,price,direction,note) VALUES('x',?,?,?)", (hi + 1000, "above", "b"))
    for bar in ReplayFeed(day_df):
        eng.on_bar(bar)
    st = {a["note"]: a["status"] for a in store.query("SELECT * FROM alerts")}
    assert st == {"a": "triggered", "b": "active"}
