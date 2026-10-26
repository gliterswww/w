import copy

import numpy as np
import pytest

from nqtrader.fills import CostModel
from nqtrader.instruments import NQ
from nqtrader.research.engine import BacktestConfig, Signal, Strategy, run_session, simulate
from nqtrader.research.strategies import ALL, CANDIDATES
from nqtrader.research.synthetic import generate_bars
from nqtrader.data import split_sessions
from nqtrader.research.walkforward import WalkForwardConfig, evaluate_strategy, verdict

CFG = BacktestConfig(cost=CostModel(NQ))


@pytest.mark.parametrize("cls", CANDIDATES, ids=lambda c: c.name)
def test_no_lookahead(cls, random_walk_sessions):
    """Scrambling the future must not change any signal emitted before it."""
    for s in random_walk_sessions[20:30]:
        cut = 200
        _, sig_a, _, _ = simulate(cls(**cls.param_grid()[0]), s, CFG, live=True)
        s2 = copy.deepcopy(s)
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 50, len(s2) - cut)
        for arr in (s2.open, s2.high, s2.low, s2.close):
            arr[cut:] += noise
        s2.high[cut:] = np.maximum.reduce([s2.open[cut:], s2.high[cut:], s2.close[cut:]])
        s2.low[cut:] = np.minimum.reduce([s2.open[cut:], s2.low[cut:], s2.close[cut:]])
        s2.volume[cut:] *= 3
        _, sig_b, _, _ = simulate(cls(**cls.param_grid()[0]), s2, CFG, live=True)
        cutoff = str(s.ts[cut - 1])[11:16]
        assert [x for x in sig_a if x["time"] < cutoff] == [x for x in sig_b if x["time"] < cutoff]


class _OnceAt(Strategy):
    name = "test_once"

    def on_bar(self, v):
        if v.minute[-1] == 10 * 60:
            c = float(v.close[-1])
            return Signal(side=1, stop=c - 1000, target=None)
        return None


def test_entry_at_next_open_and_eod_flatten(random_walk_sessions):
    s = random_walk_sessions[30]
    [t] = run_session(_OnceAt(), s, CFG)
    i = int(np.searchsorted(s.minute, 10 * 60))
    assert t.entry == pytest.approx(s.open[i + 1] + 0.25)          # next bar open + 1 tick
    assert t.reason == "time" and t.exit_time == "15:55"


def test_invalid_stop_rejected(random_walk_sessions):
    class Bad(_OnceAt):
        def on_bar(self, v):
            if v.minute[-1] == 600:
                return Signal(side=1, stop=float(v.close[-1]) + 5)    # stop above a long entry
    assert run_session(Bad(), random_walk_sessions[30], CFG) == []


def test_all_strategies_run(random_walk_sessions):
    for name, cls in ALL.items():
        for params in cls.param_grid()[:2]:
            for s in random_walk_sessions[15:25]:
                for t in run_session(cls(**params), s, CFG):
                    assert t.risk_points > 0 or name.startswith("baseline")
                    assert t.exit_time >= t.entry_time


def test_pipeline_rejects_random_walk():
    sessions = split_sessions(generate_bars(700, seed=3))
    r = evaluate_strategy(ALL["orb"], sessions, CFG, WalkForwardConfig())
    assert verdict(r, None)["status"] != "candidate"


def test_pipeline_detects_injected_edge():
    sessions = split_sessions(generate_bars(700, seed=3, edge="orb", edge_strength=0.02))
    r = evaluate_strategy(ALL["orb"], sessions, CFG, WalkForwardConfig())
    v = verdict(r, None)
    assert v["status"] == "candidate", v
    assert r["holdout"]["net_pnl"] > 0


def test_walkforward_never_uses_holdout(random_walk_sessions):
    wf = WalkForwardConfig(train_sessions=40, test_sessions=10, holdout_frac=0.25)
    r = evaluate_strategy(ALL["orb"], random_walk_sessions, CFG, wf)
    n_dev = int(len(random_walk_sessions) * 0.75)
    last_dev_day = str(random_walk_sessions[n_dev - 1].day)
    assert all(f["test_period"][1] <= last_dev_day for f in r["folds"])
    assert all(f["train_period"][1] < f["test_period"][0] for f in r["folds"])
