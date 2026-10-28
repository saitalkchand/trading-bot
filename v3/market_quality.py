"""
Market-quality / risk filter — the crude agent upgraded from a binary veto to a graded view.

Returns market_quality, trend_quality, volatility_risk, event_risk, liquidity_quality,
confidence (all 0–100) and a decision: ALLOW / REDUCE_SIZE / WAIT / BLOCK.

Crude still acts directionally: a big Brent move AGAINST the trade caps the decision at WAIT
(rising oil is bad for Indian equities, so it argues against calls, and vice versa).
"""
import math

THRESHOLDS = dict(allow=75, reduce=60, wait=40)   # >=75 ALLOW, 60-74 REDUCE_SIZE, 40-59 WAIT, <40 BLOCK


def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def assess(r, side_dir, crude_pct=None, vix=None, vix_chg=None, news_risk="LOW", crude_block_pct=1.0,
           thresholds=None):
    th = thresholds or THRESHOLDS
    have = 0; total = 5
    adx = r["adx"]
    align = (abs(r["slope50"]) > 0.15) + (r["htf_dir"] != 0) + (abs(r["vwap_dist"]) > 0.3)
    trend_q = min(100, max(0, (adx - 15) / 20 * 70) + align * 10)
    have += 1

    vr = 50.0
    if _ok(r["atr_rank"]):
        vr = 100 * max(0.0, (r["atr_rank"] - 0.5) * 2)
    if _ok(r["bar_range_atr"]) and r["bar_range_atr"] > 2:
        vr = min(100, vr + 20)
    if _ok(vix):
        have += 1
        vr = min(100, vr + (25 if vix >= 0.20 else 10 if vix >= 0.17 else 0))
        if _ok(vix_chg) and vix_chg >= 8:
            vr = min(100, vr + 20)
    have += 1

    ev = {"LOW": 0, "MEDIUM": 50, "HIGH": 100}.get(news_risk, 0)
    against = False
    if _ok(crude_pct):
        have += 1
        ev = max(ev, min(100, abs(crude_pct) * 40))
        against = (side_dir > 0 and crude_pct >= crude_block_pct) or (side_dir < 0 and crude_pct <= -crude_block_pct)
    if r.get("expiry_day"):
        ev = min(100, ev + 15)

    t = r["tod"]
    lq = 100 if 570 <= t <= 690 else 75 if t <= 810 else 55
    if _ok(r["vol_ratio"]):
        have += 1
        lq = min(100, lq * (0.8 + 0.2 * min(1.5, r["vol_ratio"])))

    mq = 0.35 * trend_q + 0.25 * lq + 0.20 * (100 - vr) + 0.20 * (100 - ev)
    dec = "ALLOW" if mq >= th["allow"] else "REDUCE_SIZE" if mq >= th["reduce"] else "WAIT" if mq >= th["wait"] else "BLOCK"
    if against and dec in ("ALLOW", "REDUCE_SIZE"):
        dec = "WAIT"
    return dict(market_quality=round(mq), trend_quality=round(trend_q), volatility_risk=round(vr),
                event_risk=round(ev), liquidity_quality=round(lq), confidence=round(100 * have / total),
                decision=dec, crude_against=against)
