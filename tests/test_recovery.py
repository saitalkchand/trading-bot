"""Restart safety: positions persist, Zerodha is the source of truth on a live restart, one bot copy only."""
import subprocess
from datetime import date, datetime

import bot
import config
from conftest import FakeKite

IST = bot.IST


def pos(sym="NIFTY26101425000CE", qty=65, sl_id=None):
    return dict(sym=sym, key=f"NFO:{sym}", exchange="NFO", qty=qty, entry=200.0, side="CE",
                t=datetime(2026, 10, 15, 10, 5, tzinfo=IST), idx_entry=25000.0, sl=24970.0, sl0=24970.0,
                target=25060.0, risk=30.0, be_done=False, why="t", best=25000.0, cost_pts=3, cost_unit=2,
                dstop=170.0, sl_id=sl_id)


def test_positions_round_trip(workdir):
    bot.save_positions({"NIFTY": pos()})
    back = bot.load_positions()
    assert back["NIFTY"]["qty"] == 65 and back["NIFTY"]["t"].hour == 10


def test_positions_from_other_mode_are_not_resumed(workdir, monkeypatch):
    bot.save_positions({"NIFTY": pos()})
    monkeypatch.setattr(config, "MODE", "live" if config.MODE == "paper" else "paper")
    assert bot.load_positions() == {}


def row(sym, qty, **kw):
    return dict(tradingsymbol=sym, exchange="NFO", product="MIS", quantity=qty, **kw)


def test_reconcile_keeps_matching_position_and_checks_stop(clock):
    k = FakeKite(["sl"])
    b = bot.Broker(k, live=True)
    sl = b.disaster_stop("NFO", "NIFTY26101425000CE", 65, 170)
    k.net = [row("NIFTY26101425000CE", 65)]
    keep, closed, problems = bot.reconcile(b, {"NIFTY": pos(sl_id=sl)}, {"NFO"})
    assert keep["NIFTY"]["sl_id"] == sl and not closed and not problems


def test_reconcile_clears_dead_stop_so_it_is_replaced(clock):
    k = FakeKite(["reject"])
    b = bot.Broker(k, live=True)
    sl = b._order("NFO", "NIFTY26101425000CE", "SELL", 65, order_type="SL", trigger_price=170, price=160)
    k.net = [row("NIFTY26101425000CE", 65)]
    keep, _, _ = bot.reconcile(b, {"NIFTY": pos(sl_id=sl)}, {"NFO"})
    assert keep["NIFTY"]["sl_id"] is None


def test_reconcile_quantity_mismatch_follows_zerodha():
    b = bot.Broker(FakeKite(), live=True)
    b.kite.net = [row("NIFTY26101425000CE", 30)]
    keep, _, problems = bot.reconcile(b, {"NIFTY": pos()}, {"NFO"})
    assert keep["NIFTY"]["qty"] == 30 and problems


def test_reconcile_detects_closed_missing_short_and_unknown():
    b = bot.Broker(FakeKite(), live=True)
    b.kite.net = [row("A", 0, sell_price=210.0), row("C", -65), row("D", 65),
                  dict(row("E", 50), product="NRML")]  # not MIS: ignored
    saved = {"N1": pos("A"), "N2": pos("B"), "N3": pos("C")}
    keep, closed, problems = bot.reconcile(b, saved, {"NFO"})
    assert keep == {}
    assert [c[0] for c in closed] == ["N1"]
    text = " ".join(problems)
    assert "B was open" in text and "SHORT" in text and "D: 65 open" in text and "E:" not in text


def test_second_copy_refused(workdir):
    p = subprocess.Popen(["sleep", "30"])
    try:
        open("x.pid", "w").write(str(p.pid))
        assert bot.acquire_lock("x.pid", "sleep") is False
        assert bot.acquire_lock("x.pid", "bot.py") is True   # pid alive but not a bot: stale file, take over
    finally:
        p.kill()


def test_dead_pid_file_is_taken_over(workdir):
    open("x.pid", "w").write("999999")
    assert bot.acquire_lock("x.pid", "bot.py") is True
