"""
Per-bar features for one index. Every value at bar i uses only bars <= i (no look-ahead):
rolling windows look back, swing points are confirmed only after the bars that define them
have closed, and higher-timeframe values come from completed higher-timeframe bars only.
"""
import numpy as np
import pandas as pd


def _wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def build(df, vol, tf=5, htf_k=3, expiry_weekday=1):
    """df: o,h,l,c indexed by IST timestamps (bar start). vol: ETF volume Series (same timestamps)."""
    f = df.copy()
    c, h, l, o = f.c, f.h, f.l, f.o
    f["date"] = f.index.date
    f["tod"] = f.index.hour * 60 + f.index.minute + tf          # bar CLOSE time, minutes after midnight
    f["bar_no"] = f.groupby("date").cumcount()

    for n in (9, 21, 50):
        f[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f["atr"] = _wilder(tr, 14)
    d = c.diff()
    rs = _wilder(d.clip(lower=0), 14) / _wilder((-d).clip(lower=0), 14).replace(0, np.nan)
    f["rsi"] = (100 - 100 / (1 + rs)).fillna(100)
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_w = _wilder(tr, 14)
    f["pdi"] = 100 * _wilder(pd.Series(pdm, f.index), 14) / atr_w
    f["mdi"] = 100 * _wilder(pd.Series(mdm, f.index), 14) / atr_w
    dx = 100 * (f.pdi - f.mdi).abs() / (f.pdi + f.mdi).replace(0, np.nan)
    f["adx"] = _wilder(dx.fillna(0), 14)
    f["adx_rising"] = f.adx > f.adx.shift(3)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    f["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    f["macd_hist_chg"] = f.macd_hist.diff()
    mid, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    f["bbw"] = 4 * sd / mid
    look = 300 if tf <= 5 else 120
    f["bbw_rank"] = f.bbw.rolling(look, min_periods=60).rank(pct=True)
    f["atr_pct"] = f.atr / c * 100
    f["atr_rank"] = f.atr_pct.rolling(look, min_periods=60).rank(pct=True)
    f["slope50"] = (f.ema50 - f.ema50.shift(6)) / f.atr          # in ATRs per 6 bars
    f["slope21"] = (f.ema21 - f.ema21.shift(3)) / f.atr
    f["range12"] = (h.rolling(12).max() - l.rolling(12).min()) / f.atr
    f["ext21"] = (c - f.ema21) / f.atr
    rng = (h - l).replace(0, np.nan)
    f["body"] = ((c - o).abs() / rng).fillna(0)
    f["bar_range_atr"] = (h - l) / f.atr

    # ---- volume proxy (ETF) vs the same time of day over the previous 5 sessions
    v = vol.reindex(f.index).astype(float)
    f["vol"] = v
    tmp = pd.DataFrame(dict(v=v, tod=f.tod, date=f.date))
    f["vol_ratio"] = v / tmp.groupby("tod").v.transform(lambda s: s.shift(1).rolling(5, min_periods=2).mean())

    # ---- session VWAP of the index weighted by ETF volume (falls back to equal weights)
    w = v.fillna(0).where(v.fillna(0) > 0, 1.0)
    tp = (h + l + c) / 3
    f["vwap"] = (tp * w).groupby(f.date).cumsum() / w.groupby(f.date).cumsum()
    f["vwap_dist"] = (c - f.vwap) / f.atr

    # ---- day context: open, opening range (first 15 min, known only after it completes), prior day
    f["day_open"] = o.groupby(f.date).transform("first")
    f["day_move"] = (c - f.day_open) / f.atr
    or_bars = max(1, 15 // tf)
    first = f.bar_no < or_bars
    orh = h.where(first).groupby(f.date).transform("max")
    orl = l.where(first).groupby(f.date).transform("min")
    f["or_high"] = orh.where(f.bar_no >= or_bars - 1)
    f["or_low"] = orl.where(f.bar_no >= or_bars - 1)
    daily = f.groupby("date").agg(dh=("h", "max"), dl=("l", "min"), dc=("c", "last"))
    prev = daily.shift(1)
    f["pdh"] = f.date.map(prev.dh)
    f["pdl"] = f.date.map(prev.dl)
    f["pdc"] = f.date.map(prev.dc)
    f["gap"] = (f.day_open - f.pdc) / f.atr

    # ---- swing points (2 bars each side), usable only once confirmed (2 bars later)
    hh = h.values; ll = l.values; n = len(f)
    sh = np.full(n, np.nan); sl = np.full(n, np.nan); sh2 = np.full(n, np.nan); sl2 = np.full(n, np.nan)
    last_h = [np.nan, np.nan]; last_l = [np.nan, np.nan]
    for i in range(n):
        j = i - 2  # candidate pivot confirmed at bar i
        if j >= 2:
            if hh[j] >= max(hh[j - 2:j]) and hh[j] > max(hh[j + 1:i + 1]):
                last_h = [hh[j], last_h[0]]
            if ll[j] <= min(ll[j - 2:j]) and ll[j] < min(ll[j + 1:i + 1]):
                last_l = [ll[j], last_l[0]]
        sh[i], sh2[i], sl[i], sl2[i] = last_h[0], last_h[1], last_l[0], last_l[1]
    f["swing_high"], f["swing_high2"], f["swing_low"], f["swing_low2"] = sh, sh2, sl, sl2

    # ---- higher timeframe: groups of htf_k bars within each session, completed groups only
    g = f.bar_no // htf_k
    complete = (f.bar_no % htf_k) == htf_k - 1
    hc = c[complete]
    he = hc.ewm(span=20, adjust=False).mean()
    hdir = np.sign(he - he.shift(2)) * ((hc > he) == (he > he.shift(2)))  # +1 up, -1 down, 0 mixed
    f["htf_ema"] = he.reindex(f.index).ffill()
    f["htf_dir"] = hdir.reindex(f.index).ffill().fillna(0)

    # ---- calendar
    f["expiry_day"] = f.index.weekday == expiry_weekday
    f["minutes_from_open"] = f.tod - (9 * 60 + 15)
    return f
