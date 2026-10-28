"""Volume confirmation, VIX-scaled exits and the global-markets agent — pure functions, no network."""
from datetime import datetime
from zoneinfo import ZoneInfo

import strategy
from agents import debate, global_markets as gm

IST = ZoneInfo("Asia/Kolkata")
SIG = dict(side="CE", entry=25000.0, sl=24980.0, target=25040.0, risk=20.0, why="bull cross")


# ---------------------------------------------------------------- volume
def test_relative_volume_per_minute():
    ts = [i * 60 for i in range(30)]
    vols = [100] * 25 + [300] * 5           # last 5-min candle trades 3x normal
    rv, n = strategy.relative_volume(ts, vols, 25 * 60, 5, 4)
    assert n == 5 and abs(rv - 3.0) < 1e-9


def test_relative_volume_counts_a_partly_published_candle():
    ts = [i * 60 for i in range(28)]        # only 3 of the candle's 5 minutes are out yet
    vols = [100] * 25 + [200] * 3
    rv, n = strategy.relative_volume(ts, vols, 25 * 60, 5, 4)
    assert n == 3 and abs(rv - 2.0) < 1e-9


def test_relative_volume_needs_data():
    assert strategy.relative_volume([0, 60], [1, 1], 0, 5, 20)[0] is None
    assert strategy.relative_volume([], [], 0, 5, 20) == (None, 0)


# ---------------------------------------------------------------- VIX
def test_vix_at_base_leaves_the_signal_alone():
    s = strategy.vix_scale(SIG, 14.0, dict(VIX_BASE=14.0, RR=2.0))
    assert s["risk"] == 20.0 and s["sl"] == 24980.0 and s["target"] == 25040.0


def test_high_vix_widens_stop_and_target_together_and_is_capped():
    s = strategy.vix_scale(SIG, 21.0, dict(VIX_BASE=14.0, RR=2.0, VIX_MULT_MAX=1.3))
    f = (21 / 14) ** 0.5
    assert abs(s["risk"] - 20 * f) < 1e-9 and abs((s["target"] - s["entry"]) - 2 * s["risk"]) < 1e-9
    assert strategy.vix_scale(SIG, 60.0, dict(VIX_BASE=14.0, RR=2.0, VIX_MULT_MAX=1.3))["vix_mult"] == 1.3


def test_low_vix_tightens_a_put_and_is_floored():
    pe = dict(SIG, side="PE", sl=25020.0, target=24960.0)
    s = strategy.vix_scale(pe, 5.0, dict(VIX_BASE=14.0, RR=2.0, VIX_MULT_MIN=0.85))
    assert s["vix_mult"] == 0.85 and s["sl"] > s["entry"] > s["target"]


def test_missing_vix_is_a_no_op():
    assert strategy.vix_scale(SIG, None, {}) is SIG


# ---------------------------------------------------------------- global markets
def test_session_status_open_lunch_closed_weekend():
    tokyo = [("09:00", "11:30"), ("12:30", "15:30")]
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    assert gm.session_status("Asia/Tokyo", tokyo, at("2026-10-28 10:00"))["status"] == "open"
    assert gm.session_status("Asia/Tokyo", tokyo, at("2026-10-28 12:00"))["status"] == "break"
    st = gm.session_status("Asia/Tokyo", tokyo, at("2026-10-28 16:00"))
    assert st["status"] == "closed" and st["detail"].startswith("opens in 17 h")
    assert gm.session_status("Asia/Tokyo", tokyo, at("2026-10-31 10:00"))["status"] == "closed"   # Saturday
    assert st["opens_ist"] == "05:30" and st["closes_ist"] == "12:00"


def test_gift_night_session_runs_past_midnight_and_us_hours_follow_dst():
    gift = [("06:30", "15:40"), ("16:35", "02:45")]
    ist = lambda s: datetime.fromisoformat(s).replace(tzinfo=IST)
    assert gm.session_status("Asia/Kolkata", gift, ist("2026-10-29 01:30"))["status"] == "open"
    assert gm.session_status("Asia/Kolkata", gift, ist("2026-10-31 01:30"))["status"] == "open"   # Fri night
    assert gm.session_status("Asia/Kolkata", gift, ist("2026-10-29 16:00"))["status"] == "break"
    us = [("09:30", "16:00")]
    summer = gm.session_status("America/New_York", us, ist("2026-10-28 12:00"))
    winter = gm.session_status("America/New_York", us, ist("2026-11-10 12:00"))
    assert (summer["opens_ist"], winter["opens_ist"]) == ("19:00", "20:00")


def _mk(key, region, pct, price=100.0):
    return dict(key=key, name=key, region=region, change_pct=pct, price=price, status="closed")


def test_global_read_risk_on_and_off():
    up = [_mk("gift", "India", 0.8, 25100), _mk("sp500", "US", 1.0), _mk("nasdaq", "US", 1.0),
          _mk("nikkei", "Asia", 1.0), _mk("dax", "Europe", 1.0)]
    r = gm.analyse(up, [dict(key="dxy", change_pct=-0.5, change=-0.5, price=100)], nifty_spot=25000)
    assert r["ok"] and r["score"] > 0.6 and r["gift_premium"] == 100 and r["note"].startswith("Risk-on")
    down = [dict(m, change_pct=-m["change_pct"]) for m in up]
    assert gm.analyse(down, [])["score"] < -0.6


def test_global_veto_and_debate_weight():
    cfg = dict(USE_CRUDE_AGENT=False, USE_NEWS_AGENT=False, USE_GLOBAL_AGENT=True, GLOBAL_BLOCK_SCORE=0.6)
    bear = dict(ok=True, score=-0.7)
    assert "global" in strategy.agent_veto("CE", None, None, cfg, bear)
    assert strategy.agent_veto("PE", None, None, cfg, bear) is None
    assert strategy.agent_veto("CE", None, None, dict(cfg, USE_GLOBAL_AGENT=False), bear) is None
    assert abs(sum(debate.TRUST.values()) - 1) < 1e-9
    d = debate.run({"global": dict(ok=True, score=0.5, bull=[["US up", 0.5]], bear=[])})
    assert d["margin"] > 0 and "global" in d["analysts"]
