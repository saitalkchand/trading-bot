"""Hard risk limits: values preserved from the repo, enforced on NET P&L, and surviving restarts."""
import json
from datetime import date

import pytest

import bot
import config
import strategy
from agents import risk_manager


def test_limits_match_the_repository_values():
    # these are the user's live limits — new code must never change them silently
    assert config.MAX_DAILY_LOSS == 3000
    assert config.MAX_TRADES_PER_DAY == 3
    assert config.MAX_CONSECUTIVE_LOSSES == 2
    assert config.MAX_PREMIUM_PER_TRADE == 40000
    assert config.MAX_OPEN_POSITIONS == 1
    assert config.SCALP_ALLOW_LIVE is False


def test_daily_loss_locks(workdir):
    r = bot.Risk()
    r.record(-3000.5)
    assert not r.can_trade() and "daily loss" in r.s["reason"]


def test_two_losses_in_a_row_lock(workdir):
    r = bot.Risk()
    r.record(-100)
    assert r.can_trade()
    r.record(-100)
    assert not r.can_trade()


def test_win_resets_loss_streak_and_max_trades_locks(workdir):
    r = bot.Risk()
    r.record(-100); r.record(50); r.record(-100)
    assert r.s["trades"] == 3 and not r.can_trade() and "max trades" in r.s["reason"]


def test_lock_survives_restart_but_not_a_new_day(workdir):
    r = bot.Risk()
    r.record(-3500)
    assert not bot.Risk().can_trade()
    s = json.load(open("state.json")); s["date"] = "2000-01-01"; json.dump(s, open("state.json", "w"))
    assert bot.Risk().can_trade()


SIG = dict(side="CE", entry=25000, sl=24970, target=25060, risk=30, why="test")


def review(premium, qty=65, state=None, crude=None, news=None):
    state = state or dict(pnl=0, locked=False)
    return risk_manager.review(SIG, qty, premium, state, 0, crude, news, None, config)


def test_premium_cap_vetoes():
    assert review(600)["approved"]                       # 600 x 65 = ₹39,000 — inside ₹40,000
    rm = review(700)                                     # ₹45,500 — over
    assert not rm["approved"] and any(c["name"] == "Premium cap" and not c["ok"] for c in rm["checks"])


def test_loss_budget_uses_what_is_left_today():
    ok = review(300, state=dict(pnl=0, locked=False))
    low = review(300, state=dict(pnl=-2000, locked=False))   # 65 x 0.7 x 30 = 1,365 at the stop; 1,000 left
    assert ok["approved"] and not low["approved"]


def test_locked_day_is_vetoed():
    assert not review(300, state=dict(pnl=0, locked=True, reason="x"))["approved"]


def test_crude_veto_blocks_ce_not_pe():
    crude = dict(change_pct=1.5)                         # Brent +1.5% vs the 1% threshold
    assert not review(300, crude=crude)["approved"]
    assert strategy.agent_veto("PE", crude, None, config) is None


def test_charges_are_sane():
    ch = strategy.charges(200, 210, 65)
    assert 50 < ch < 100        # ~₹40 brokerage + STT + exchange + GST on 13k turnover
    assert strategy.charges(200, 210, 0) == 0
