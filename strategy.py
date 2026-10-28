"""
Strategy v2 — shared by bot.py (live/paper) and backtest.py, so both use identical rules.

Why v2 (from the v1 backtest: -₹13,340, 38% win rate, avg win ≈ avg loss):
  * v1 lost mostly on whipsaws (11 'opposite signal' + 9 'square-off' exits).
  * v1's win size ≈ loss size, so it needed >50% wins to profit.

v2 rules (all thresholds in config.py / Settings page):
  ENTRY (on a closed 5-min candle of the index)
    CE (bullish)  : EMA9 > EMA21 with a fresh cross in the last CROSS_LOOKBACK candles
                    + close > EMA50 and EMA50 rising        (trade with the trend only)
                    + RSI between RSI_BUY_CE and RSI_MAX    (momentum, but not exhausted)
                    + bullish candle closing above previous high (confirmation)
                    + ATR >= MIN_ATR_PCT of price            (skip dead/choppy markets)
    PE (bearish)  : mirror image.
  RISK : stop = recent swing (last 3 candles) on the INDEX, clamped to 0.6–1.5 × ATR
  REWARD: target = entry ± RR × risk  (RR = 2 → 1:2)
  MANAGE: at +BREAKEVEN_AT_R × risk (1:1), stop moves to COST — entry plus the points needed to
          pay charges and slippage, so a 1:1 trade can no longer become a loss; optional trailing
          stop and time stop (see manage)
  EXIT : stop, target, or square-off time. No 'opposite signal' exits.
  VOLUME (VOLUME_FILTER): the signal candle's volume (NIFTYBEES proxy) must be >= VOLUME_MIN_RVOL x normal
  VIX (VIX_ADJUST_EXITS): stop and target scaled by sqrt(India VIX / VIX_BASE) — see vix_scale
  AGENT FILTERS: crude-oil, news and global-markets agents can veto a side (see agents/).
"""


def ema_series(values, n):
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out  # aligned to values[n-1:]


def rsi(values, n=14):
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(candles, n=14):
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(candles, n=14):
    """Wilder's ADX — trend strength (not direction). < 20 = choppy, > 25 = trending."""
    if len(candles) < 2 * n + 1:
        return None
    tr, pdm, mdm = [], [], []
    for a, b in zip(candles[:-1], candles[1:]):
        up, dn = b["h"] - a["h"], a["l"] - b["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])))
    atr_, p, m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for i in range(n, len(tr) + 1):
        if i > n:
            atr_ = atr_ - atr_ / n + tr[i - 1]
            p = p - p / n + pdm[i - 1]
            m = m - m / n + mdm[i - 1]
        pdi, mdi = (100 * p / atr_, 100 * m / atr_) if atr_ else (0, 0)
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def resample(candles, k):
    """Group k candles into one (5-min → 15-min with k=3). Drops the incomplete first group."""
    start = len(candles) % k
    return [dict(o=g[0]["o"], h=max(c["h"] for c in g), l=min(c["l"] for c in g), c=g[-1]["c"])
            for g in (candles[i:i + k] for i in range(start, len(candles), k))]


def signal_v2(candles, cfg):
    """
    candles: list of dicts {o,h,l,c} — completed candles only, oldest first.
    cfg: object/dict with the settings below.
    Returns dict(side, entry, sl, target, risk, why) or None.
    """
    g = (lambda k: cfg[k]) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k))
    slow_n = g("EMA_TREND")
    if len(candles) < slow_n + 10:
        return None
    closes = [c["c"] for c in candles]
    fast, slow, trend = ema_series(closes, g("EMA_FAST")), ema_series(closes, g("EMA_SLOW")), ema_series(closes, slow_n)
    look = g("CROSS_LOOKBACK")
    diff = [f - s for f, s in zip(fast[-(look + 1):], slow[-(look + 1):])]
    r = rsi(closes[-200:], g("RSI_PERIOD"))
    a = atr(candles[-100:], 14)
    if r is None or a is None:
        return None
    cur, prev = candles[-1], candles[-2]
    price = cur["c"]
    trend_up = price > trend[-1] and trend[-1] > trend[-6]
    trend_dn = price < trend[-1] and trend[-1] < trend[-6]
    if a < price * g("MIN_ATR_PCT") / 100:
        return None
    swing = candles[-3:]
    rr = g("RR")
    opt = (lambda k, d: cfg.get(k, d)) if isinstance(cfg, dict) else (lambda k, d: getattr(cfg, k, d))
    adx_min = opt("ADX_MIN", 0)
    x = adx(candles[-150:], 14) if adx_min else None
    if adx_min and (x is None or x < adx_min):
        return None
    htf_up = htf_dn = True
    if opt("HTF_FILTER", False):  # 15-min EMA-20 must point the same way
        h = resample(candles, 3)
        e = ema_series([c["c"] for c in h], 20)
        if len(e) < 3:
            return None
        htf_up = h[-1]["c"] > e[-1] and e[-1] > e[-3]
        htf_dn = h[-1]["c"] < e[-1] and e[-1] < e[-3]
    trend_up, trend_dn = trend_up and htf_up, trend_dn and htf_dn

    crossed_up = diff[-1] > 0 and any(d <= 0 for d in diff[:-1])
    crossed_dn = diff[-1] < 0 and any(d >= 0 for d in diff[:-1])

    if (crossed_up and trend_up and g("RSI_BUY_CE") <= r <= g("RSI_MAX")
            and cur["c"] > cur["o"] and cur["c"] > prev["h"]):
        risk = price - min(c["l"] for c in swing)
        risk = min(max(risk, 0.6 * a), 1.5 * a)
        return dict(side="CE", entry=price, sl=price - risk, target=price + rr * risk, risk=risk,
                    why=f"bull cross, trend up, RSI {r:.0f}, ATR {a:.1f}")
    if (crossed_dn and trend_dn and (100 - g("RSI_MAX")) <= r <= g("RSI_BUY_PE")
            and cur["c"] < cur["o"] and cur["c"] < prev["l"]):
        risk = max(c["h"] for c in swing) - price
        risk = min(max(risk, 0.6 * a), 1.5 * a)
        return dict(side="PE", entry=price, sl=price + risk, target=price - rr * risk, risk=risk,
                    why=f"bear cross, trend down, RSI {r:.0f}, ATR {a:.1f}")
    return None


def signal_volume(candles, cfg):
    """
    Volume-spike breakout (scalper). Indices have no volume, so each candle carries "v" = volume of a
    proxy that trades the same basket (NIFTYBEES by default) and "day" = its date, for session VWAP.
      CE : volume >= SCALP_VOL_MULT x average of the previous SCALP_VOL_AVG_N candles
           + close above the highest high of the previous SCALP_VOL_BREAKOUT_N candles (breakout)
           + bullish candle with a body >= half its range (buyers in control, not a wick)
           + close above session VWAP and above EMA_SLOW                (with the intraday trend)
           + ATR >= MIN_ATR_PCT of price
      PE : mirror image.
    Same return shape as signal_v2. Returns None if volume is missing for the candles it needs.
    """
    g = (lambda k, d=None: cfg.get(k, d)) if isinstance(cfg, dict) else (lambda k, d=None: getattr(cfg, k, d))
    avg_n, brk_n, mult = int(g("SCALP_VOL_AVG_N", 20)), int(g("SCALP_VOL_BREAKOUT_N", 5)), g("SCALP_VOL_MULT", 2.0)
    if len(candles) < max(avg_n, brk_n, g("EMA_SLOW")) + 2:
        return None
    cur, prior = candles[-1], candles[-1 - avg_n:-1]
    vols = [c.get("v") for c in prior]
    if not cur.get("v") or any(v is None for v in vols):
        return None
    avg = sum(vols) / avg_n
    if avg <= 0:
        return None
    rvol = cur["v"] / avg
    if rvol < mult:
        return None
    a = atr(candles[-100:], 14)
    price = cur["c"]
    if a is None or a < price * g("MIN_ATR_PCT") / 100:
        return None
    session = [c for c in candles if c.get("day") == cur.get("day")] if cur.get("day") is not None else candles[-60:]
    pv = sum((c["h"] + c["l"] + c["c"]) / 3 * (c.get("v") or 0) for c in session)
    vv = sum(c.get("v") or 0 for c in session)
    vwap = pv / vv if vv else price
    ema = ema_series([c["c"] for c in candles[-200:]], g("EMA_SLOW"))[-1]
    rng = cur["h"] - cur["l"]
    if rng <= 0 or abs(cur["c"] - cur["o"]) < 0.5 * rng:
        return None
    box = candles[-1 - brk_n:-1]
    rr = g("RR")
    why = f"volume {rvol:.1f}x avg, VWAP {vwap:.1f}, ATR {a:.1f}"
    if cur["c"] > cur["o"] and price > max(c["h"] for c in box) and price > vwap and price > ema:
        risk = min(max(price - cur["l"], 0.6 * a), 1.5 * a)
        return dict(side="CE", entry=price, sl=price - risk, target=price + rr * risk, risk=risk,
                    why="bull breakout on " + why)
    if cur["c"] < cur["o"] and price < min(c["l"] for c in box) and price < vwap and price < ema:
        risk = min(max(cur["h"] - price, 0.6 * a), 1.5 * a)
        return dict(side="PE", entry=price, sl=price + risk, target=price - rr * risk, risk=risk,
                    why="bear breakdown on " + why)
    return None


def relative_volume(ts, vols, start, minutes, avg_n):
    """Volume of the signal candle vs normal, from a proxy's 1-min bars (indices have no volume).
    ts: epoch seconds of each 1-min bar, vols: their volumes, start: epoch of the signal candle's open.
    Compares volume PER MINUTE, so a candle whose last minute Yahoo hasn't published yet still counts.
    Returns (rvol, minutes found) or (None, minutes found) when there isn't enough data."""
    end = start + minutes * 60
    cur = [v for t, v in zip(ts, vols) if start <= t < end]
    prior = [v for t, v in zip(ts, vols) if t < start][-avg_n * minutes:]
    if len(cur) < min(2, minutes) or len(prior) < minutes * 3:
        return None, len(cur)
    base = sum(prior) / len(prior)
    return ((sum(cur) / len(cur)) / base if base > 0 else None), len(cur)


def vix_scale(sig, vix, cfg):
    """Widen the stop and target when India VIX is high, tighten them when it is low.
    factor = sqrt(VIX / VIX_BASE), clamped to [VIX_MULT_MIN, VIX_MULT_MAX]; target stays RR x the new risk.
    The square root damps it because the stop already scales with ATR. Returns a new signal dict."""
    g = (lambda k, d: cfg.get(k, d)) if isinstance(cfg, dict) else (lambda k, d: getattr(cfg, k, d))
    if not vix or vix <= 0:
        return sig
    f = min(max((vix / g("VIX_BASE", 14.0)) ** 0.5, g("VIX_MULT_MIN", 0.85)), g("VIX_MULT_MAX", 1.3))
    d = 1 if sig["side"] == "CE" else -1
    risk = sig["risk"] * f
    return dict(sig, risk=risk, sl=sig["entry"] - d * risk, target=sig["entry"] + d * g("RR", 2.0) * risk,
                vix=round(vix, 2), vix_mult=round(f, 2), why=sig["why"] + f", VIX {vix:.1f} → stop ×{f:.2f}")


def scalp_signal(candles, cfg):
    """The scalper's entry rule, chosen by SCALP_STRATEGY: "ema" (signal_v2) or "volume" (signal_volume)."""
    g = (lambda k, d: cfg.get(k, d)) if isinstance(cfg, dict) else (lambda k, d: getattr(cfg, k, d))
    return signal_volume(candles, cfg) if g("SCALP_STRATEGY", "ema") == "volume" else signal_v2(candles, cfg)


def itm_strike(spot, side, step, itm_strikes):
    """In-the-money strike at least `itm_strikes` FULL strike steps inside the money.
    CE: highest strike <= spot - itm_strikes x step.  PE: lowest strike >= spot + itm_strikes x step.
    (Rounding to the nearest strike first could leave a '1 strike ITM' option only half a step deep,
    which an ordinary pullback turns out-of-the-money.)"""
    import math
    itm_strikes = max(1, int(itm_strikes))
    if side == "CE":
        return math.floor((spot - itm_strikes * step) / step) * step
    return math.ceil((spot + itm_strikes * step) / step) * step


def cost_per_unit(premium, qty):
    """Rupees per option unit to cover round-trip charges and slippage (premium break-even)."""
    if not qty:
        return 0.0
    buy = premium * qty
    # Zerodha options (Oct 2026): ₹20/order, STT 0.15% of sell premium (from 1 Apr 2026),
    # NSE txn 0.03553% (BSE 0.0325%), stamp 0.003% on buy, GST 18% on brokerage + txn
    ch = 40 + 0.0015 * buy + 0.0003553 * 2 * buy + 0.00003 * buy + 0.18 * (40 + 0.0003553 * 2 * buy)
    return ch / qty + 0.004 * premium * 2  # ~0.4% slippage each way on a liquid ITM strike


def charges(buy_px, sell_px, qty, exchange="NFO"):
    """Estimated round-trip charges in rupees for an option buy then sell (no slippage —
    real fills already include it). Same rates as cost_per_unit, plus SEBI fees."""
    if not qty:
        return 0.0
    buy, sell = buy_px * qty, sell_px * qty
    txn = (0.000325 if exchange == "BFO" else 0.0003553) * (buy + sell)
    sebi = 0.000001 * (buy + sell)
    brokerage = 40
    return round(brokerage + 0.0015 * sell + txn + 0.00003 * buy + sebi + 0.18 * (brokerage + txn + sebi), 2)


def cost_points(premium, qty, delta=0.7):
    """Index points the trade must gain just to pay its round-trip charges and slippage.
    Used to put the stop at true 'cost' (not index entry) once the trade reaches 1:1."""
    return cost_per_unit(premium, qty) / delta


def manage(pos, hi, lo, cfg, now=None, close=None):
    """
    Update stop/target on the INDEX for one new price range [lo, hi].
    pos: dict(side, entry, sl, target, risk, be_done, t, cost_pts, best)
    Returns (exit_level, reason) or (None, None). Stop is checked first (conservative),
    and stop moves only take effect from the next price update.

      BREAKEVEN_AT_R  at +R × this, stop → cost (index entry + the points needed to pay charges)
      TRAIL_START_R   from +R × this, stop trails TRAIL_R × risk behind the best price (0 = off)
      TIME_STOP_MIN   exit if the trade hasn't reached +0.5R within this many minutes (0 = off)
    """
    g = (lambda k: cfg.get(k, 0)) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k, 0))
    up = pos["side"] == "CE"
    d = 1 if up else -1
    if (up and lo <= pos["sl"]) or (not up and hi >= pos["sl"]):
        moved = pos.get("trailed") or pos.get("be_done")
        return pos["sl"], ("trailing stop" if pos.get("trailed") else "stop at cost" if moved else "stop-loss")
    if (up and hi >= pos["target"]) or (not up and lo <= pos["target"]):
        return pos["target"], f"target 1:{g('RR'):g}"

    best = max(pos.get("best", pos["entry"]), hi) if up else min(pos.get("best", pos["entry"]), lo)
    pos["best"] = best
    gained = (best - pos["entry"]) * d / pos["risk"]  # best open profit, in R

    be = g("BREAKEVEN_AT_R")
    if be and not pos.get("be_done") and gained >= be:
        cost = pos["entry"] + d * pos.get("cost_pts", 0)
        if (cost - pos["sl"]) * d > 0:
            pos["sl"] = cost
        pos["be_done"] = True
    ts, tr = g("TRAIL_START_R"), g("TRAIL_R")
    if ts and tr and gained >= ts:
        trail = best - d * tr * pos["risk"]
        if (trail - pos["sl"]) * d > 0:
            pos["sl"], pos["trailed"] = trail, True
    tm = g("TIME_STOP_MIN")
    if tm and now is not None and pos.get("t") is not None and gained < 0.5:
        if (now - pos["t"]).total_seconds() >= tm * 60:
            return (close if close is not None else (hi + lo) / 2), f"time stop ({tm:g} min)"
    return None, None


def agent_veto(side, crude, news, cfg, glob=None):
    """Return a reason string if the agents block this side, else None."""
    import time as _t
    g = (lambda k: cfg[k]) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k))
    fresh = lambda d, age: d and (d.get("fetched_at") is None or _t.time() - d["fetched_at"] < age)
    crude = crude if fresh(crude, 1800) else None   # ignore data older than 30 min
    news = news if fresh(news, 3600) else None
    glob = glob if fresh(glob, 1800) else None
    if g("USE_CRUDE_AGENT") and crude and crude.get("change_pct") is not None:
        ch, th = crude["change_pct"], g("CRUDE_BLOCK_PCT")
        if side == "CE" and ch >= th:
            return f"crude agent: Brent +{ch:.2f}% (bearish for India)"
        if side == "PE" and ch <= -th:
            return f"crude agent: Brent {ch:.2f}% (bullish for India)"
    if g("USE_NEWS_AGENT") and news and news.get("ok"):
        if g("NEWS_BLOCK_ON_EVENT") and news.get("event_risk"):
            return "news agent: high-impact event today (" + ", ".join(news["events"][:2]) + ")"
        s, th = news.get("score", 0), g("NEWS_BLOCK_SCORE")
        if side == "CE" and s <= -th:
            return f"news agent: sentiment {s:+.2f} (bearish)"
        if side == "PE" and s >= th:
            return f"news agent: sentiment {s:+.2f} (bullish)"
    opt = (lambda k, d: cfg.get(k, d)) if isinstance(cfg, dict) else (lambda k, d: getattr(cfg, k, d))
    if opt("USE_GLOBAL_AGENT", False) and glob and glob.get("ok"):
        s, th = glob.get("score", 0), opt("GLOBAL_BLOCK_SCORE", 0.6)
        if side == "CE" and s <= -th:
            return f"global markets agent: {s:+.2f} (risk-off worldwide)"
        if side == "PE" and s >= th:
            return f"global markets agent: {s:+.2f} (risk-on worldwide)"
    return None
