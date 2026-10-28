"""Order handling: fills are what Zerodha filled, never what was requested; paper never reaches the broker."""
import pytest

import bot
from conftest import FakeKite


def live(plan=None):
    k = FakeKite(plan)
    return k, bot.Broker(k, live=True)


def test_paper_never_sends_orders(clock):
    k = FakeKite()
    b = bot.Broker(k, live=False)
    assert b.buy("NFO", "X", 65, 100).qty == 65
    assert b.sell("NFO", "X", 65, 101, sl_id="whatever").qty == 65
    assert b.disaster_stop("NFO", "X", 65, 80) is None
    b.move_stop("S1", 90)
    assert k.calls == []
    with pytest.raises(RuntimeError):
        b._order("NFO", "X", "BUY", 65, order_type="LIMIT", price=1)


def test_full_fill(clock):
    k, b = live(["fill"])
    f = b.buy("NFO", "X", 65, 100)
    assert (f.qty, f.status) == (65, "COMPLETE") and f.price == pytest.approx(101.0)


def test_partial_fill_then_timeout_keeps_filled_quantity(clock):
    k, b = live(["partial:30"])
    f = b.buy("NFO", "X", 65, 100)
    assert f.qty == 30 and f.status == "CANCELLED"
    assert ("cancel", "O1") in k.calls


def test_rejected_buy_is_falsy(clock):
    k, b = live(["reject"])
    assert not b.buy("NFO", "X", 65, 100)


def test_uncancellable_buy_reports_open_status(clock):
    k, b = live(["uncancellable"])
    f = b.buy("NFO", "X", 65, 100)
    assert f.status not in bot.TERMINAL


def test_errored_request_that_reached_zerodha_is_not_sent_twice(clock):
    k, b = live(["raise_placed"])
    f = b.buy("NFO", "X", 65, 100)
    assert f.qty == 65
    assert sum(1 for c in k.calls if c[0] == "place") == 1


def test_errored_request_that_never_arrived_raises(clock):
    k, b = live(["raise_lost"])
    with pytest.raises(TimeoutError):
        b.buy("NFO", "X", 65, 100)


def test_tags_are_unique_and_valid(clock):
    b = bot.Broker(FakeKite(), live=True)
    tags = {b._new_tag() for _ in range(50)}
    assert len(tags) == 50 and all(len(t) <= 20 and t.isalnum() for t in tags)


def test_sell_when_exchange_stop_already_filled_sends_nothing_more(clock):
    k, b = live(["sl"])
    sl = b.disaster_stop("NFO", "X", 65, 80)
    k.fill(sl, 79.5)
    f = b.sell("NFO", "X", 65, 79, sl_id=sl)
    assert (f.qty, f.price) == (65, 79.5)
    assert sum(1 for c in k.calls if c[0] == "place") == 1  # only the stop itself


def test_sell_refuses_second_order_when_stop_cannot_be_cancelled(clock):
    k, b = live(["uncancellable"])
    sl = b._order("NFO", "X", "SELL", 65, order_type="SL", trigger_price=80, price=76)
    f = b.sell("NFO", "X", 65, 90, sl_id=sl)
    assert f.status == "STOP_STILL_OPEN" and f.qty == 0
    assert sum(1 for c in k.calls if c[0] == "place") == 1


def test_sell_after_partly_filled_stop_sells_only_the_rest(clock):
    k, b = live(["partial:20", "fill"])
    sl = b._order("NFO", "X", "SELL", 65, order_type="SL", trigger_price=80, price=76)
    f = b.sell("NFO", "X", 65, 90, sl_id=sl)
    exit_order = [c[1] for c in k.calls if c[0] == "place"][1]
    assert exit_order["quantity"] == 45
    assert f.qty == 65 and f.status == "COMPLETE"


def test_partial_exit_reports_incomplete(clock):
    k, b = live(["partial:40"])
    f = b.sell("NFO", "X", 65, 90)
    assert f.qty == 40 and f.status != "COMPLETE"


def test_exchange_stop_never_moves_down(clock):
    k, b = live(["sl"])
    sl = b.disaster_stop("NFO", "X", 65, 80)
    b.move_stop(sl, 75)
    assert not any(c[0] == "modify" for c in k.calls)
    b.move_stop(sl, 95)
    b.move_stop(sl, 90)
    mods = [c for c in k.calls if c[0] == "modify"]
    assert len(mods) == 1 and mods[0][2]["trigger_price"] == 95
