"""End-to-end: bot.main() in LIVE mode against the fake broker (no real orders, no network).

Scenario: entry fills, the exchange stop request is lost, the bot still tracks the position, re-places the
stop, then the kill switch exits it — and the trade is booked net of charges into the risk state."""
import csv
import json
import os
import time as real_time
import types
from datetime import date, datetime, timedelta

import pytest

import bot
import config
import kiteconnect
from agents import debate, pipeline, risk_manager, technical, trader
from conftest import Clock, FakeKite

SYM = "NIFTY26102024950CE"


class LiveKite(FakeKite):
    def __init__(self, plan):
        super().__init__(plan, price=200.0)

    def set_access_token(self, _):
        pass

    def historical_data(self, *a, **k):
        raise RuntimeError("no history in tests")

    def instruments(self, ex):
        return [dict(name="NIFTY", instrument_type="CE", strike=24950.0, expiry=date.today() + timedelta(days=5),
                     tradingsymbol=SYM, lot_size=65, exchange="NFO")]


def run_main(monkeypatch, plan, stop_after, net=None, signal=True):
    clock = Clock()
    start = datetime.combine(date.today(), datetime.min.time(), tzinfo=bot.IST).replace(hour=10)
    start -= timedelta(days=max(0, start.weekday() - 4))     # tests on a weekend run as Friday
    monkeypatch.setattr(bot, "now", lambda: start + timedelta(seconds=clock.t - 1_700_000_000.0))
    calls = {"n": 0}

    def sleep(s):
        clock.sleep(s)
        calls["n"] += 1
        if calls["n"] == stop_after:
            open("STOP", "w").close()
    monkeypatch.setattr(bot, "time", types.SimpleNamespace(time=clock.time, sleep=sleep))

    k = LiveKite(plan)
    # an exchange stop from before the restart that is no longer live (e.g. cancelled in Kite)
    k.orders_["GONE"] = dict(state=dict(status="CANCELLED", filled_quantity=0, average_price=0), how="", tag="", kw={})
    k._ltp = {"NSE:NIFTY 50": 25000.0}
    k.net = net or []
    monkeypatch.setattr(kiteconnect, "KiteConnect", lambda api_key=None: k)
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "TRADE_INSTRUMENTS", "NIFTY")
    monkeypatch.setattr(config, "NO_ENTRY_BEFORE", "09:15")
    monkeypatch.setattr(config, "CANDLE_MINUTES", 1)
    monkeypatch.setattr(config, "VOLUME_FILTER", False)     # both fetch from Yahoo; tested in test_filters.py
    monkeypatch.setattr(config, "VIX_ADJUST_EXITS", False)
    monkeypatch.setenv("BOT_CONFIRM_LIVE", "YES")
    open("access_token.txt", "w").write("x")

    sig = dict(side="CE", entry=25000.0, sl=24970.0, target=25060.0, risk=30.0, why="test signal")
    monkeypatch.setattr(bot.strategy, "signal_v2", lambda c, cfg: dict(sig) if signal else None)
    monkeypatch.setattr(pipeline, "refresh", lambda cfg, force=False: real_time.sleep(3600) or [])
    monkeypatch.setattr(pipeline, "reports", lambda cfg: {})
    monkeypatch.setattr(pipeline, "record", lambda *a, **k: None)
    monkeypatch.setattr(technical, "analyse", lambda *a, **k: {})
    monkeypatch.setattr(debate, "run", lambda *a, **k: dict(margin=0.0, verdict="", flags=[], challenges=[]))
    monkeypatch.setattr(trader, "decide", lambda *a, **k: dict(action="BUY CE", conviction=0.5, reason=""))
    bot.main()
    return k


def test_lost_stop_order_never_orphans_the_position(workdir, monkeypatch):
    # buy fills · first stop request lost · retry stop rests · exit order fills
    k = run_main(monkeypatch, ["fill", "raise_lost", "sl", "fill"], stop_after=40)
    places = [c[1] for c in k.calls if c[0] == "place"]
    kinds = [(p["transaction_type"], p["order_type"]) for p in places]
    assert kinds == [("BUY", "LIMIT"), ("SELL", "SL"), ("SELL", "SL"), ("SELL", "LIMIT")]
    assert ("cancel", "O2") in k.calls                          # the resting stop is cancelled before exiting
    rows = list(csv.DictReader(open("trades.csv")))
    assert len(rows) == 1 and rows[0]["reason"] == "kill switch"
    assert float(rows[0]["net_pnl"]) == pytest.approx(float(rows[0]["pnl"]) - float(rows[0]["charges"]))
    assert json.load(open("positions.json"))["positions"] == {}
    state = json.load(open("state.json"))
    assert state["trades"] == 1 and state["pnl"] == pytest.approx(float(rows[0]["net_pnl"]))


def test_partial_entry_tracks_only_filled_quantity(workdir, monkeypatch):
    k = run_main(monkeypatch, ["partial:30", "sl", "fill"], stop_after=40)
    places = [c[1] for c in k.calls if c[0] == "place"]
    assert places[1]["order_type"] == "SL" and places[1]["quantity"] == 30
    assert places[2]["quantity"] == 30                          # exit sells what was bought, never 65
    assert list(csv.DictReader(open("trades.csv")))[0]["qty"] == "30"


def test_live_restart_resumes_position_zerodha_still_holds(workdir, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    held = dict(sym=SYM, key=f"NFO:{SYM}", exchange="NFO", qty=65, entry=190.0, side="CE",
                t=datetime.now(bot.IST), idx_entry=25000.0, sl=24900.0, sl0=24900.0, target=25200.0, risk=30.0,
                be_done=False, why="before restart", best=25000.0, cost_pts=3, cost_unit=2, dstop=160.0,
                sl_id="GONE")
    bot.save_positions({"NIFTY": held})
    net = [dict(tradingsymbol=SYM, exchange="NFO", product="MIS", quantity=65)]
    k2 = run_main(monkeypatch, ["sl", "fill"], stop_after=8, net=net, signal=False)
    places = [(c[1]["transaction_type"], c[1]["order_type"], c[1]["quantity"]) for c in k2.calls if c[0] == "place"]
    assert places == [("SELL", "SL", 65), ("SELL", "LIMIT", 65)]   # stop re-placed, then exited; no new buy
    row = list(csv.DictReader(open("trades.csv")))[0]
    assert row["why"] == "before restart" and row["entry"] == "190.0"
