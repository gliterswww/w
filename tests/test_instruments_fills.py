from datetime import date

from nqtrader.fills import CostModel, check_exit, market_fill
from nqtrader.instruments import MNQ, NQ, front_contract, next_contract, third_friday


def test_third_friday():
    assert third_friday(2026, 12) == date(2026, 12, 18)
    assert third_friday(2027, 3) == date(2027, 3, 19)


def test_front_contract_rolls_eight_days_before_expiry():
    assert front_contract("NQ", date(2026, 10, 26)).symbol == "NQZ6"
    assert front_contract("NQ", date(2026, 12, 9)).symbol == "NQZ6"
    assert front_contract("NQ", date(2026, 12, 10)).symbol == "NQH7"   # roll date
    assert front_contract("MNQ", date(2026, 12, 31)).symbol == "MNQH7"
    assert next_contract(front_contract("NQ", date(2026, 10, 26))).symbol == "NQH7"


def test_tick_rounding_and_values():
    assert NQ.round_to_tick(20000.13) == 20000.25 or NQ.round_to_tick(20000.13) == 20000.0
    assert NQ.tick_value == 5.0 and MNQ.tick_value == 0.5


def test_market_fill_pays_slippage():
    c = CostModel(NQ, slippage_ticks=1)
    assert market_fill(1, 100.0, c) == 100.25
    assert market_fill(-1, 100.0, c) == 99.75


def test_stop_gap_fills_at_open_not_stop():
    c = CostModel(NQ, slippage_ticks=1)
    # long, stop 95, bar gaps down to open 90
    assert check_exit(1, 95.0, 110.0, o=90.0, h=91.0, l=89.0, cost=c) == ("stop", 89.75)


def test_stop_wins_when_bar_hits_both():
    c = CostModel(NQ, slippage_ticks=0)
    assert check_exit(1, 95.0, 105.0, o=100.0, h=106.0, l=94.0, cost=c)[0] == "stop"


def test_target_needs_trade_through():
    c = CostModel(NQ, slippage_ticks=0)
    assert check_exit(1, 95.0, 105.0, o=100.0, h=105.0, l=99.0, cost=c) is None        # touch only
    assert check_exit(1, 95.0, 105.0, o=100.0, h=105.25, l=99.0, cost=c) == ("target", 105.0)
    assert check_exit(-1, 105.0, 95.0, o=100.0, h=101.0, l=94.75, cost=c) == ("target", 95.0)


def test_pnl_includes_commission():
    c = CostModel(NQ, commission_rt=4.0)
    assert c.pnl_usd(1, 100.0, 101.0, qty=2) == 2 * 20.0 - 8.0
