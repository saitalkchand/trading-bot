"""
Trade-quality score, 0–100, for a proposed CE (dir=+1) or PE (dir=-1) at bar i.
Each component is 0..1; the score is the weighted average × 100. Weights are configurable
and were set BEFORE testing (they are not fitted to the data).
"""
import math

WEIGHTS = dict(trend=20, momentum=15, volume=15, structure=15, volatility=10, htf=10, entry=10, time=5, liquidity=0)


def _nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def components(r, d, risk_pts=None):
    """r: feature row (dict-like) at the signal bar; d: +1 CE / -1 PE."""
    c = r["c"]
    comp = {}
    comp["trend"] = (((r["ema9"] - r["ema21"]) * d > 0) + ((r["ema21"] - r["ema50"]) * d > 0)
                     + ((c - r["ema50"]) * d > 0) + (r["slope50"] * d > 0.1)
                     + (r["adx"] >= 20 and (r["pdi"] - r["mdi"]) * d > 0)) / 5
    rsi = r["rsi"] if d > 0 else 100 - r["rsi"]
    mom_rsi = 1.0 if 55 <= rsi <= 70 else 0.5 if (50 <= rsi < 55 or 70 < rsi <= 75) else 0.0
    mom_macd = (r["macd_hist"] * d > 0) * 0.5 + (r["macd_hist_chg"] * d > 0) * 0.5
    comp["momentum"] = (mom_rsi + mom_macd) / 2
    vr = r["vol_ratio"]
    comp["volume"] = 0.5 if _nan(vr) else 1.0 if vr >= 1.3 else 0.6 if vr >= 1.0 else 0.3 if vr >= 0.7 else 0.0
    if d > 0:
        hh = not _nan(r["swing_high2"]) and r["swing_high"] > r["swing_high2"]
        hl = not _nan(r["swing_low2"]) and r["swing_low"] > r["swing_low2"]
        levels = [x for x in (r["pdh"], r["or_high"], r["swing_high"]) if not _nan(x) and x > c]
        room = (min(levels) - c) / r["atr"] if levels else 9
    else:
        hh = not _nan(r["swing_low2"]) and r["swing_low"] < r["swing_low2"]
        hl = not _nan(r["swing_high2"]) and r["swing_high"] < r["swing_high2"]
        levels = [x for x in (r["pdl"], r["or_low"], r["swing_low"]) if not _nan(x) and x < c]
        room = (c - max(levels)) / r["atr"] if levels else 9
    need = 1.5 if risk_pts is None else 1.5 * risk_pts / r["atr"]
    comp["structure"] = ((hh + hl) / 2 + (1.0 if room >= need else 0.5 if room >= need / 2 else 0.0)) / 2
    ar = r["atr_rank"]
    comp["volatility"] = 0.5 if _nan(ar) else 1.0 if 0.25 <= ar <= 0.85 else 0.5 if 0.15 <= ar <= 0.95 else 0.0
    comp["htf"] = 1.0 if r["htf_dir"] * d > 0 else 0.5 if r["htf_dir"] == 0 else 0.0
    ext = abs(r["ext21"])
    e1 = 1.0 if ext <= 1.0 else 0.5 if ext <= 1.75 else 0.0
    vd = r["vwap_dist"] * d
    e2 = 1.0 if 0 < vd <= 2.0 else 0.5 if vd > 2.0 else 0.0
    e3 = 1.0 if r["body"] >= 0.5 else 0.5
    comp["entry"] = (e1 + e2 + e3) / 3
    t = r["tod"]  # bar close, minutes after midnight — a-priori prior, checked by the time analysis
    comp["time"] = 1.0 if 570 <= t <= 660 else 0.6 if t <= 780 else 0.3 if t <= 840 else 0.0
    comp["liquidity"] = 0.5  # no option bid/ask history available — neutral placeholder
    return comp


def score(r, d, weights=None, risk_pts=None):
    w = weights or WEIGHTS
    comp = components(r, d, risk_pts)
    tot = sum(w.values()) or 1
    return 100 * sum(comp[k] * w.get(k, 0) for k in comp) / tot, comp
