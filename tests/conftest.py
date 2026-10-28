"""Shared test fixtures: a scripted fake Kite broker and a fake clock. No test talks to Zerodha or Yahoo."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class FakeKite:
    """Scripted stand-in for KiteConnect. plan: behaviour of each next placed order —
    fill | partial:<n> | reject | open | uncancellable | sl | sl_filled | raise_placed | raise_lost"""

    def __init__(self, plan=None, price=100.0):
        self.plan, self.price = list(plan or []), price
        self.orders_, self.calls, self.net = {}, [], []
        self._ltp = {}

    # --- market data
    def ltp(self, keys):
        return {k: {"last_price": self._ltp.get(k, self.price)} for k in keys}

    def positions(self):
        return {"net": self.net, "day": []}

    # --- orders
    def place_order(self, **kw):
        self.calls.append(("place", kw))
        how = self.plan.pop(0) if self.plan else "fill"
        qty, px = kw["quantity"], kw.get("price") or kw.get("trigger_price") or self.price
        st = dict(status="COMPLETE", filled_quantity=qty, average_price=px, status_message=None)
        if how.startswith("partial"):
            st.update(status="OPEN", filled_quantity=int(how.split(":")[1]))
        elif how == "reject":
            st.update(status="REJECTED", filled_quantity=0, average_price=0, status_message="margin")
        elif how in ("open", "uncancellable"):
            st.update(status="OPEN", filled_quantity=0, average_price=0)
        elif how == "sl":
            st.update(status="TRIGGER PENDING", filled_quantity=0, average_price=0)
        if how == "raise_lost":
            raise TimeoutError("read timeout")
        self._n = getattr(self, "_n", 0) + 1
        oid = f"O{self._n}"
        self.orders_[oid] = dict(state=st, how=how, tag=kw.get("tag"), kw=kw)
        if how == "raise_placed":
            raise TimeoutError("read timeout")
        return oid

    def orders(self):
        return [dict(order_id=o, tag=v["tag"], **v["state"]) for o, v in self.orders_.items()]

    def order_history(self, oid):
        return [dict(self.orders_[oid]["state"])]

    def cancel_order(self, variety, oid):
        self.calls.append(("cancel", oid))
        o = self.orders_[oid]
        if o["how"] == "uncancellable" or o["state"]["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
            raise RuntimeError("order cannot be cancelled")
        o["state"]["status"] = "CANCELLED"

    def modify_order(self, variety, oid, **kw):
        self.calls.append(("modify", oid, kw))
        self.orders_[oid]["state"].update(kw)

    def fill(self, oid, avg):
        """Make a resting order (e.g. an exchange stop) execute."""
        o = self.orders_[oid]
        o["state"].update(status="COMPLETE", filled_quantity=o["kw"]["quantity"], average_price=avg)


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    import bot
    c = Clock()
    monkeypatch.setattr(bot, "time", types.SimpleNamespace(time=c.time, sleep=c.sleep))
    return c


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    """Every test runs in an empty folder, so logs, state, trade and position files never touch the real ones."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
