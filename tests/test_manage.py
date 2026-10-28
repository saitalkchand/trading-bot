"""Trade management invariants on the existing strategy.manage(): the stop never moves against the position."""
import random

import pytest

import strategy

CFG = dict(RR=2.0, BREAKEVEN_AT_R=1.0, TRAIL_START_R=1.0, TRAIL_R=1.0, TIME_STOP_MIN=0)


@pytest.mark.parametrize("side", ["CE", "PE"])
@pytest.mark.parametrize("seed", range(40))
def test_stop_never_widens(side, seed):
    rnd = random.Random(seed)
    d = 1 if side == "CE" else -1
    entry, risk = 25000.0, 30.0
    pos = dict(side=side, entry=entry, sl=entry - d * risk, target=entry + d * 2 * risk, risk=risk,
               be_done=False, cost_pts=4.0, best=entry)
    target0, price = pos["target"], entry
    for _ in range(300):
        price += rnd.gauss(0, 6)
        prev = pos["sl"]
        lvl, why = strategy.manage(pos, price, price, CFG)
        assert (pos["sl"] - prev) * d >= 0, f"stop moved against the trade: {prev} -> {pos['sl']}"
        assert pos["target"] == target0
        if lvl is not None:
            break


def test_breakeven_moves_stop_to_cost_not_entry():
    pos = dict(side="CE", entry=100.0, sl=90.0, target=120.0, risk=10.0, be_done=False, cost_pts=1.5, best=100.0)
    strategy.manage(pos, 110.0, 110.0, dict(CFG, TRAIL_START_R=0))
    assert pos["be_done"] and pos["sl"] == pytest.approx(101.5)


def test_stop_checked_before_target_on_same_bar():
    pos = dict(side="CE", entry=100.0, sl=90.0, target=120.0, risk=10.0, be_done=False, cost_pts=0, best=100.0)
    lvl, why = strategy.manage(pos, 125.0, 85.0, CFG)
    assert lvl == 90.0 and why == "stop-loss"
