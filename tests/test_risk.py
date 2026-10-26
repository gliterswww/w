from datetime import date, datetime, time

import pytest
from pydantic import ValidationError

from nqtrader.data import ET
from nqtrader.instruments import MNQ
from nqtrader.risk import RiskLimits, RiskManager, SystemHealth

HEALTHY = SystemHealth(broker_connected=True, positions_reconciled=True, protective_orders_confirmed=True,
                       data_fresh=True, contract_ok=True)
T = datetime(2026, 10, 26, 10, 30, tzinfo=ET)


def rm(**kw):
    return RiskManager(RiskLimits(**kw), MNQ)


def check(r, **kw):
    args = dict(now=T, side=1, price=20000.0, stop=19980.0, open_positions=0, health=HEALTHY,
                kill_switch=False, news_blackout=None)
    args.update(kw)
    return r.check_entry(**args)


def test_allows_clean_entry_and_sizes_by_risk():
    d = check(rm(risk_per_trade_usd=150))
    # per contract: (20 pts + 4 ticks * 0.25) * $2 + $1.50 = $43.50 -> 3 contracts, capped at max_order_size 2
    assert d.allowed and d.qty == 2


def test_stop_too_wide_blocks():
    d = check(rm(risk_per_trade_usd=150), stop=19900.0)   # 100 pts on MNQ = $203.50 > $150
    assert not d.allowed and any("too wide" in r for r in d.reasons)


@pytest.mark.parametrize("field", ["broker_connected", "positions_reconciled", "protective_orders_confirmed",
                                   "data_fresh", "contract_ok"])
def test_any_unconfirmed_state_blocks(field):
    h = SystemHealth(**{**HEALTHY.__dict__, field: False})
    assert not check(rm(), health=h).allowed


def test_kill_switch_news_window_and_positions_block():
    assert not check(rm(), kill_switch=True).allowed
    assert not check(rm(), news_blackout="CPI").allowed
    assert not check(rm(), open_positions=1).allowed
    assert not check(rm(), now=T.replace(hour=15, minute=45)).allowed
    assert not check(rm(), stop=20010.0).allowed   # wrong side


def test_daily_loss_lockout_then_next_day_clear():
    r = rm(max_daily_loss_usd=300, max_weekly_loss_usd=1000, max_monthly_loss_usd=2000)
    r.record_trade(T.date(), -310)
    assert not check(r).allowed
    nxt = datetime(2026, 10, 27, 10, 30, tzinfo=ET)
    assert check(r, now=nxt).allowed


def test_consecutive_losses_is_a_daily_breaker():
    r = rm(max_consecutive_losses=2, max_daily_loss_usd=1000, max_weekly_loss_usd=5000, max_monthly_loss_usd=9000,
           max_total_drawdown_usd=9000)
    r.record_trade(T.date(), -50)
    r.record_trade(T.date(), -50)
    assert any("consecutive" in x for x in check(r).reasons)
    assert check(r, now=datetime(2026, 10, 27, 10, 30, tzinfo=ET)).allowed


def test_weekly_and_drawdown_budgets():
    r = rm(max_daily_loss_usd=400, max_weekly_loss_usd=500, max_monthly_loss_usd=2000)
    r.record_trade(date(2026, 10, 26), -300)
    r.record_trade(date(2026, 10, 27), -250)
    assert any("Weekly" in x for x in check(r, now=datetime(2026, 10, 28, 10, 0, tzinfo=ET)).reasons)


def test_size_never_grows_after_losses():
    r = rm(risk_per_trade_usd=150)
    before = r.position_size(20)
    for _ in range(2):
        r.record_trade(T.date(), -100)
    assert r.position_size(20) == before


def test_profit_lock():
    r = rm(daily_profit_lock_usd=200)
    r.record_trade(T.date(), 250)
    assert any("profit lock" in x for x in check(r).reasons)


def test_limits_validation():
    with pytest.raises(ValidationError):
        RiskLimits(risk_per_trade_usd=5000)                  # > 3% of capital
    with pytest.raises(ValidationError):
        RiskLimits(max_daily_loss_usd=5000)                  # daily > weekly
    with pytest.raises(ValidationError):
        RiskLimits(max_order_size=5, max_contracts=2)
    with pytest.raises(ValidationError):
        RiskLimits(no_entries_after=time(16, 30))
    with pytest.raises(ValidationError):
        RiskLimits(unknown_field=1)


def test_slippage_check():
    r = rm(max_slippage_ticks=2)
    assert r.check_fill_slippage(100.0, 100.5, 1) is None
    assert r.check_fill_slippage(100.0, 101.0, 1) is not None
