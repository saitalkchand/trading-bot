"""Free paper feed refuses stale prices instead of serving a cached one as live."""
import time

import pytest

import feed


def chart_with(age, stale=False):
    d = dict(t=[time.time() - age], o=[1.0], h=[1.0], l=[1.0], c=[25000.0], v=[0])
    if stale:
        d["stale"] = True
    return lambda *a, **k: d


def test_fresh_price(monkeypatch):
    monkeypatch.setattr(feed.market, "chart", chart_with(70))
    monkeypatch.setattr(feed, "_market_open", lambda: True)
    assert feed.FreeFeed().ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"] == 25000.0


def test_old_bar_during_market_hours_raises(monkeypatch):
    monkeypatch.setattr(feed.market, "chart", chart_with(900))
    monkeypatch.setattr(feed, "_market_open", lambda: True)
    with pytest.raises(feed.StaleData):
        feed.FreeFeed().ltp(["NSE:NIFTY 50"])


def test_cached_fallback_copy_raises(monkeypatch):
    monkeypatch.setattr(feed.market, "chart", chart_with(30, stale=True))
    monkeypatch.setattr(feed, "_market_open", lambda: True)
    with pytest.raises(feed.StaleData):
        feed.FreeFeed().ltp(["NSE:NIFTY 50"])


def test_free_feed_cannot_place_orders():
    with pytest.raises(RuntimeError):
        feed.FreeFeed().place_order(tradingsymbol="X")
