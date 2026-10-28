"""Strategy lab: detectors, features, regime, exits, sizing, engine reproducibility, paper-only isolation."""
import json
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from conftest import ROOT
from scalp_lab import exits as X
from scalp_lab import regime as RG
from scalp_lab.features import Features
from scalp_lab.sizing import lots_for_risk
from scalp_lab.strategies import BreakoutRetest, SweepReversal

IST = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 10, 5, 9, 15, tzinfo=IST)


def flat(n, price=25000.0, v=1000.0):
    """n quiet candles: range 10 points around price."""
    return [dict(o=price, h=price + 5, l=price - 5, c=price, v=v) for _ in range(n)]


def stamp(bars):
    return [dict(b, t=T0 + timedelta(minutes=i)) for i, b in enumerate(bars)]


def run(det, bars):
    F, out = Features(), []
    for i, b in enumerate(stamp(bars)):
        F.update(b)
        s = det.update(F)
        if s:
            out.append((i, s))
    return out, F


def sweep_series(reclaim=True):
    bars = flat(70)
    bars[62] = dict(o=25000, h=25005, l=24985, c=25000, v=1000)            # swing low 24985 (confirmed at bar 65)
    bars.append(dict(o=25000, h=25002, l=24980, c=24990, v=1500))          # 70: sweeps it by 5 pts (0.5 ATR)
    if reclaim:
        bars.append(dict(o=24990, h=25001, l=24989, c=25000, v=1200))      # 71: strong close back above
    else:
        bars += [dict(o=24990, h=24992, l=24982, c=24984, v=900) for _ in range(5)]    # stays below the level
    return bars


# ---------------------------------------------------------------- liquidity sweep
def test_sweep_then_reclaim_gives_ce_on_the_reclaim_candle():
    sigs, _ = run(SweepReversal(dict(SWEEP_MIN_REWARD_R=0)), sweep_series())
    assert [(i, s["side"]) for i, s in sigs] == [(71, "CE")]
    s = sigs[0][1]
    assert s["setup"]["level"] == 24985 and s["idx_stop"] < 24980        # stop beyond the sweep extreme


def test_a_touch_is_not_a_sweep():
    bars = sweep_series()
    bars[70] = dict(o=25000, h=25002, l=24984.5, c=24990, v=1500)         # 0.5 pt beyond: < 0.1 ATR
    sigs, _ = run(SweepReversal(dict(SWEEP_MIN_REWARD_R=0)), bars)
    assert sigs == []


def test_no_reclaim_expires_and_level_is_not_reused():
    det = SweepReversal(dict(SWEEP_MIN_REWARD_R=0))
    sigs, _ = run(det, sweep_series(reclaim=False))
    assert sigs == []
    statuses = [s["status"] for s in det.log if s.get("level") == 24985]
    assert statuses[0] == "provisional" and statuses[-1] in ("expired", "invalidated")
    assert statuses.count("provisional") == 1                                 # one sweep → one setup


def test_not_enough_room_is_rejected_with_reason():
    det = SweepReversal()                                                     # default 1.5R room needed
    sigs, _ = run(det, sweep_series())
    assert sigs == [] and any(s["status"] == "rejected" and "room" in s["reason"] for s in det.log)


def test_pivot_is_only_known_after_it_is_confirmed():
    F = Features()
    bars = stamp(sweep_series())
    for i, b in enumerate(bars[:66]):
        F.update(b)
        known = [s["price"] for s in F.swing_lows]
        assert (24985 in known) == (i >= 65), i


# ---------------------------------------------------------------- breakout / retest
def brt_series(fail=False):
    bars = flat(70)
    bars.append(dict(o=25000, h=25013, l=24999, c=25012, v=2500))           # 70: breakout above 25005
    if fail:
        bars.append(dict(o=25012, h=25012, l=24998, c=25000, v=1500))       # 71: back inside → failed
    else:
        bars.append(dict(o=25012, h=25013, l=25006, c=25008, v=1200))       # 71: retest that holds
        bars.append(dict(o=25009, h=25017, l=25008, c=25016, v=1800))       # 72: resumes above the retest high
    return bars


def test_retest_entry_only_after_resumption():
    sigs, _ = run(BreakoutRetest(dict(BRT_MIN_REWARD_R=0)), brt_series())
    assert [(i, s["side"], s["kind"]) for i, s in sigs] == [(72, "CE", "breakout retest")]


def test_failed_breakout_is_invalidated():
    det = BreakoutRetest(dict(BRT_MIN_REWARD_R=0))
    sigs, _ = run(det, brt_series(fail=True))
    assert sigs == [] and any(s["status"] == "invalidated" and "failed breakout" in s["reason"] for s in det.log)


def test_no_retest_variant_enters_on_the_breakout_close():
    sigs, _ = run(BreakoutRetest(dict(BRT_MIN_REWARD_R=0, BRT_RETEST=False)), brt_series())
    assert (sigs[0][0], sigs[0][1]["kind"]) == (70, "breakout")     # later new highs are separate breakouts


def test_volume_filter_rejects_quiet_breakout():
    det = BreakoutRetest(dict(BRT_MIN_REWARD_R=0, BRT_RVOL_MIN=3.0))
    sigs, _ = run(det, brt_series())
    assert sigs == [] and any("volume" in s["reason"] for s in det.log if s["status"] == "rejected")


# ---------------------------------------------------------------- no look-ahead
@pytest.mark.parametrize("make", [lambda: SweepReversal(dict(SWEEP_MIN_REWARD_R=0)),
                                  lambda: BreakoutRetest(dict(BRT_MIN_REWARD_R=0))])
def test_signals_identical_on_prefix_and_full_history(make):
    import random
    rnd = random.Random(3)
    p, bars = 25000.0, []
    for _ in range(400):
        o = p; c = o + rnd.gauss(0, 6)
        bars.append(dict(o=o, h=max(o, c) + abs(rnd.gauss(0, 4)), l=min(o, c) - abs(rnd.gauss(0, 4)), c=c,
                         v=abs(rnd.gauss(1000, 400))))
        p = c
    full, _ = run(make(), bars)
    for i, s in full[:15]:
        pre, _ = run(make(), bars[:i + 1])                 # only candles up to the signal exist
        assert pre and pre[-1][0] == i and pre[-1][1]["entry"] == s["entry"]


# ---------------------------------------------------------------- features / volume / regime
def test_relative_volume_uses_previous_completed_candles_only():
    F = Features(rvol_n=20)
    bars = stamp(flat(20, v=1000) + [dict(o=1, h=2, l=0.5, c=1.5, v=3000)])
    for i, b in enumerate(bars):
        F.update(b)
        if i < 20:
            assert F.rvol is None
    assert F.rvol == pytest.approx(3.0)


def test_flat_vwap_with_repeated_crosses_is_range():
    bars = flat(60)
    for k in range(40):
        up = k % 2 == 0
        bars.append(dict(o=24997 if up else 25003, h=25006, l=24994, c=25003 if up else 24997, v=1000))
    F = Features()
    for b in stamp(bars):
        F.update(b)
    assert RG.classify(F)[0] == "RANGE"
    assert RG.no_trade(F, "CE") and "RANGE" in RG.no_trade(F, "CE")


def test_candle_gap_marks_data_unreliable():
    bars = stamp(flat(70))
    bars[-1]["t"] += timedelta(minutes=6)
    F = Features()
    for b in bars:
        F.update(b)
    assert RG.classify(F)[0] == "UNRELIABLE"


# ---------------------------------------------------------------- exits
def pos(**kw):
    p = dict(side="CE", qty=65, buy=200.0, delta=0.6, cost_unit=1.5)
    p.update(kw)
    X.initial(p, dict(entry=25000, idx_stop=24990), dict(buy=p["buy"], delta=p["delta"], idx_atr=10, cost_unit=1.5),
              dict(X.EXIT_DEFAULTS, **kw.get("cfg", {})))
    return p


def test_fixed_benchmark_levels():
    p = pos()
    assert (p["stop"], p["target"], p["be_at"]) == (195.0, 210.0, 5.0)


def test_stop_never_moves_down_and_is_audited():
    p = pos()
    assert X.move_stop(p, 190, "test", "t") is False and p["stop"] == 195.0
    assert X.move_stop(p, 196.03, "test", "t") is True and p["stop"] == 196.05      # 0.05 tick
    assert p["events"][-1]["old"] == 195.0 and p["events"][-1]["risk_change"] < 0   # less money at risk


def test_structure_stop_outside_band_is_rejected_not_resized():
    p = dict(side="CE", qty=65)
    why = X.initial(p, dict(entry=25000, idx_stop=24900), dict(buy=200, delta=0.6, idx_atr=10, cost_unit=1),
                    dict(X.EXIT_DEFAULTS, EXIT_POLICY="structure"))
    assert why and "outside" in why


class _F:
    def __init__(self, bull):
        self.bar = dict(o=100, c=110 if bull else 90)
        self.ema = {9: 100}
        self.vwap = 100
        self.atr = 10


def test_target_extension_needs_momentum_and_locks_profit_first():
    cfg = dict(X.EXIT_DEFAULTS, EXTEND=True)
    p = pos(cfg=dict(EXTEND=True))
    X.update(p, 209.0, 208.0, _F(bull=False), "t1", cfg)          # near target, no momentum → unchanged
    assert p["target"] == 210.0
    X.update(p, 209.0, 208.0, _F(bull=True), "t2", cfg)
    assert p["target"] == 212.5 and p["stop"] >= 202.5              # locked +0.5R before extending
    rules = [e["rule"] for e in p["events"]]
    assert rules.index("lock before target extension") < rules.index("target extension (momentum + structure)")


def test_break_even_goes_to_cost_not_entry():
    p = pos()
    X.update(p, 205.5, 205.0, _F(True), "t", X.EXIT_DEFAULTS)
    assert p["be"] and p["stop"] == 201.5


# ---------------------------------------------------------------- sizing
def test_sizing_never_scales_up_and_rejects_unaffordable():
    assert lots_for_risk(5, 1.5, 65, 1, 10_000) == (1, "")
    assert lots_for_risk(5, 1.5, 65, 2, 500)[0] == 1
    assert lots_for_risk(5, 1.5, 65, 1, 300)[0] == 0


# ---------------------------------------------------------------- engine
@pytest.fixture(scope="module")
def market():
    from scalp_lab import data, engine
    d, _ = data.load()
    return d, engine.Market(d)


def test_engine_reproduces_existing_backtest_trade_for_trade(market):
    import scalp_backtest
    from scalp_lab import engine
    d, M = market
    with engine.legacy_config():
        old = scalp_backtest.run(d, ["NIFTY"], 1, "base", strat="volume")
    import config
    assert config.SCALP_SL_POINTS == 15           # restored to the live setting afterwards
    new = engine.run(M, dict(strategy="volume"), fills="base", compat=True)["trades"]
    assert len(old) == len(new) > 0
    assert (old[["entry_time", "exit_time", "side"]].values == new[["entry_time", "exit_time", "side"]].values).all()


def test_engine_runs_are_deterministic(market):
    from scalp_lab import engine
    _, M = market
    a = engine.run(M, dict(strategy="sweep"), fills="conservative")["trades"]
    b = engine.run(M, dict(strategy="sweep"), fills="conservative")["trades"]
    assert a.drop(columns=["events"]).equals(b.drop(columns=["events"]))


def test_lab_never_loads_the_broker():
    code = "import sys; import scalp_lab.experiments, scalp_lab.live; print('bot' in sys.modules, 'kiteconnect' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert out == "False False"


def test_scalper_refuses_experimental_strategy_live(monkeypatch):
    import config
    import scalper
    for k, v in dict(MODE="live", SCALP_ALLOW_LIVE=True, SCALP_STRATEGY="sweep").items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setenv("BOT_CONFIRM_LIVE", "YES")
    with pytest.raises(SystemExit):
        scalper.main()


def test_lab_live_only_signals_on_newest_candle_and_status_is_json():
    from scalp_lab.live import LabLive
    L = LabLive("sweep", dict(SWEEP_MIN_REWARD_R=0))
    bars = stamp(sweep_series())
    assert L.feed(bars) is not None                    # newest candle (71) is the reclaim
    assert L.feed(bars) is None                        # nothing new → no repeat signal
    L2 = LabLive("sweep", dict(SWEEP_MIN_REWARD_R=0))
    assert L2.feed(bars + stamp(flat(73))[72:]) is None   # the reclaim is no longer the newest candle
    json.dumps(L.status())
