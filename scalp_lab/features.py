"""
Incremental, completed-candle features — the single source for the lab backtest AND the live scalper.

Feed candles one at a time with update(c); every value afterwards describes the market as of that
candle's CLOSE and uses nothing later. c = dict(t=<bar start datetime>, o, h, l, c, v=<proxy volume or None>).

Volume is NIFTYBEES.NS (the index itself has none), so "vwap" is NIFTY price weighted by NIFTYBEES volume:
a proxy VWAP, not the exchange VWAP of any traded contract.

Swing pivots are only known R candles after they happen (they need R higher-low/lower-high candles to the
right). They are added to the level lists at that moment — never earlier.
"""
from collections import deque


class Features:
    def __init__(self, ema=(9, 21, 50), atr_n=14, rvol_n=20, pivot_l=3, pivot_r=3, htf_min=15, htf_ema=20,
                 level_max_age=180):
        self.ema_n = ema
        self.atr_n, self.rvol_n = atr_n, rvol_n
        self.pl, self.pr = pivot_l, pivot_r
        self.htf_min, self.htf_ema_n = htf_min, htf_ema
        self.level_max_age = level_max_age
        self.i = -1
        self.bars = deque(maxlen=400)
        self.ema = {n: None for n in ema}
        self.ema_hist = {n: deque(maxlen=30) for n in ema}
        self.atr = None
        self.atr_hist = deque(maxlen=400)
        self.vols = deque(maxlen=rvol_n)   # PREVIOUS completed candles' volume (current one excluded)
        self.rvol = None
        self.day = None
        self.pv = self.vv = 0.0
        self.vwap = None
        self.vwap_hist = deque(maxlen=60)
        self.sess_hi = self.sess_lo = None
        self.prev_hi = self.prev_lo = None
        self.swing_highs, self.swing_lows = [], []   # dict(price, i_pivot, i_known)
        self.htf_bucket, self.htf_close, self.htf_ema, self.htf_last = None, None, None, None
        self.data_ok, self.data_issue = True, ""
        self.last_t = None

    # ---------------------------------------------------------------- update
    def update(self, c):
        self.i += 1
        t = c.get("t")
        self._check_data(c, t)
        day = t.date() if t is not None else c.get("day")
        if day != self.day:                       # new session
            if self.day is not None:
                self.prev_hi, self.prev_lo = self.sess_hi, self.sess_lo
            self.day, self.pv, self.vv = day, 0.0, 0.0
            self.sess_hi, self.sess_lo = c["h"], c["l"]
            self.vwap_hist.clear()
        else:
            self.sess_hi, self.sess_lo = max(self.sess_hi, c["h"]), min(self.sess_lo, c["l"])
        prev = self.bars[-1] if self.bars else None
        # relative volume: this candle vs the average of the previous rvol_n completed candles
        v = c.get("v")
        if v is not None and len(self.vols) == self.rvol_n and all(x is not None for x in self.vols):
            avg = sum(self.vols) / self.rvol_n
            self.rvol = v / avg if avg > 0 else None
        else:
            self.rvol = None
        self.vols.append(v)
        # proxy VWAP
        tp = (c["h"] + c["l"] + c["c"]) / 3
        self.pv += tp * (v or 0)
        self.vv += v or 0
        self.vwap = self.pv / self.vv if self.vv else tp
        self.vwap_hist.append(self.vwap)
        # EMAs
        for n in self.ema_n:
            k = 2 / (n + 1)
            self.ema[n] = c["c"] if self.ema[n] is None else c["c"] * k + self.ema[n] * (1 - k)
            self.ema_hist[n].append(self.ema[n])
        # ATR (Wilder)
        tr = c["h"] - c["l"] if prev is None else max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"]))
        self.atr = tr if self.atr is None else (self.atr * (self.atr_n - 1) + tr) / self.atr_n
        self.atr_hist.append(self.atr)
        self.bars.append(dict(c, i=self.i))
        self._pivots()
        self._htf(c, t)
        self.last_t = t
        return self

    def _check_data(self, c, t):
        issue = ""
        if not (c["l"] <= min(c["o"], c["c"]) and c["h"] >= max(c["o"], c["c"])):
            issue = "bad OHLC"
        elif self.last_t is not None and t is not None and t.date() == self.last_t.date() \
                and (t - self.last_t).total_seconds() > 180:
            issue = f"{int((t - self.last_t).total_seconds() // 60)}-min gap in candles"
        self.data_ok, self.data_issue = not issue, issue
        if issue:
            self.gap_at = self.i

    def _pivots(self):
        """A pivot at bar j is confirmed once bar j+R has closed."""
        L, R = self.pl, self.pr
        if len(self.bars) < L + R + 1:
            return
        j = len(self.bars) - 1 - R
        b = self.bars[j]
        left, right = list(self.bars)[j - L:j], list(self.bars)[j + 1:]
        if all(b["h"] > x["h"] for x in left) and all(b["h"] >= x["h"] for x in right):
            self.swing_highs.append(dict(price=b["h"], i_pivot=b["i"], i_known=self.i, t=b.get("t")))
        if all(b["l"] < x["l"] for x in left) and all(b["l"] <= x["l"] for x in right):
            self.swing_lows.append(dict(price=b["l"], i_pivot=b["i"], i_known=self.i, t=b.get("t")))
        cut = self.i - self.level_max_age
        self.swing_highs = [s for s in self.swing_highs if s["i_pivot"] >= cut][-20:]
        self.swing_lows = [s for s in self.swing_lows if s["i_pivot"] >= cut][-20:]

    def _htf(self, c, t):
        """Higher timeframe: EMA of COMPLETED htf candles only. A bucket completes when its last minute closes."""
        if t is None:
            return
        m = t.hour * 60 + t.minute
        bucket = (t.date(), m // self.htf_min)
        if bucket != self.htf_bucket:
            self.htf_bucket = bucket
        if m % self.htf_min == self.htf_min - 1:          # last minute of the bucket just closed
            k = 2 / (self.htf_ema_n + 1)
            self.htf_ema = c["c"] if self.htf_ema is None else c["c"] * k + self.htf_ema * (1 - k)
            self.htf_last = c["c"]

    # ---------------------------------------------------------------- read-outs
    @property
    def bar(self):
        return self.bars[-1]

    def ready(self, n=60):
        return self.i >= n and self.atr is not None

    def htf_bias(self):
        if self.htf_ema is None or self.htf_last is None:
            return 0
        return 1 if self.htf_last > self.htf_ema else -1 if self.htf_last < self.htf_ema else 0

    def body_ratio(self, c=None):
        c = c or self.bar
        rng = c["h"] - c["l"]
        return abs(c["c"] - c["o"]) / rng if rng > 0 else 0.0

    def vwap_slope_pct(self, n=10):
        if len(self.vwap_hist) <= n:
            return None
        return (self.vwap_hist[-1] - self.vwap_hist[-1 - n]) / self.vwap_hist[-1] * 100

    def vwap_crosses(self, n=20):
        """Times the close changed side of VWAP over the last n candles (same session)."""
        bars = [b for b in list(self.bars)[-n:] if b.get("t") is None or b["t"].date() == self.day]
        side = [1 if b["c"] > w else -1 for b, w in zip(bars, list(self.vwap_hist)[-len(bars):])]
        return sum(1 for a, b in zip(side, side[1:]) if a != b)

    def atr_ratio(self, short=5, long=60):
        """Range expansion: recent ATR vs a longer baseline of ATR values."""
        h = list(self.atr_hist)
        if len(h) < long:
            return None
        base = sum(h[-long:]) / long
        return h[-1] / base if base else None

    def ema_slope_pct(self, n=21, k=5):
        h = self.ema_hist[n]
        if len(h) <= k:
            return None
        return (h[-1] - h[-1 - k]) / h[-1] * 100

    def levels(self, side):
        """Liquidity levels a sweep can take: confirmed swing lows (for CE) / highs (for PE) + previous day."""
        if side == "CE":
            lv = [dict(price=s["price"], kind="swing low", i=s["i_pivot"]) for s in self.swing_lows]
            if self.prev_lo is not None:
                lv.append(dict(price=self.prev_lo, kind="previous day low", i=-1))
        else:
            lv = [dict(price=s["price"], kind="swing high", i=s["i_pivot"]) for s in self.swing_highs]
            if self.prev_hi is not None:
                lv.append(dict(price=self.prev_hi, kind="previous day high", i=-1))
        return lv

    def opposing_liquidity(self, side, price):
        """Nearest liquidity in the trade's direction — a realistic first objective."""
        if side == "CE":
            c = [s["price"] for s in self.swing_highs if s["price"] > price] + \
                [x for x in (self.sess_hi, self.prev_hi) if x is not None and x > price]
            return min(c) if c else None
        c = [s["price"] for s in self.swing_lows if s["price"] < price] + \
            [x for x in (self.sess_lo, self.prev_lo) if x is not None and x < price]
        return max(c) if c else None
