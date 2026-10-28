"""
Scalper V2 — filtered 1-minute entries. Shared by scalper.py (live/paper) and scalp_v2_backtest.py,
so the backtest tests exactly the rules that trade.

Pipeline (every stage must pass; then the trade-quality score must reach TRADE_SCORE_MIN):

  1-min index candles (+ NIFTYBEES volume, India VIX daily, a second index for confirmation)
    → features()        indicators, all computed from completed candles only (no look-ahead)
    → regime            STRONG/WEAK UP/DOWN, RANGE, HIGH_VOLATILITY; only strong trends trade
                        (weak ones only with a very high score)
    → trend             EMA9 vs EMA21 with multi-candle slopes, price beyond EMA9, EMA21 and VWAP
    → momentum          RSI band + RSI slope, MACD above/below signal with the histogram growing
    → entry trigger     pullback to EMA9/21 that holds EMA21, then a break of the pullback high/low;
                        or a volume breakout confirmed by the next candle (false-breakout guard)
    → candle quality    body size, opposite wick, not over-extended vs ATR, market structure
    → context           VWAP distance, opening-range shock, India VIX percentile, other index not
                        strongly against
    → score             trend 20, momentum 15, VWAP 10, RSI 10, MACD 10, volume 10, breakout 10,
                        liquidity 10, volatility 5  (= 100)
  Option liquidity/spread, agents, daily risk and cooldowns are applied by the caller, because they
  need live quotes or trading state. Hard risk limits are never overridden by a score or an agent.
"""
import math

import numpy as np
import pandas as pd

REGIMES = ("STRONG_UPTREND", "WEAK_UPTREND", "RANGE", "WEAK_DOWNTREND", "STRONG_DOWNTREND", "HIGH_VOLATILITY")
WEIGHTS = dict(trend=20, momentum=15, vwap=10, rsi=10, macd=10, volume=10, breakout=10, liquidity=10, volatility=5)

DEFAULTS = dict(
    SCALP_REGIME_FILTER=True, SCALP_ADX_MIN=20, SCALP_ATR_MIN_PCT=0.025, SCALP_EMA_SEPARATION_MIN=0.03,
    SCALP_CHOP_FILTER=True, SCALP_CHOP_MAX=61.8, SCALP_MAX_EMA_CROSSES=3, SCALP_HIGHVOL_MULT=2.0,
    SCALP_ALLOW_WEAK_TREND=True, SCALP_WEAK_TREND_SCORE_MIN=85,
    SCALP_EMA_SLOPE_LOOKBACK=3,
    SCALP_PULLBACK_ENTRY=True, SCALP_PULLBACK_LOOKBACK=3, SCALP_PULLBACK_TOL_PCT=0.03,
    SCALP_BREAKOUT_ENTRY=True, SCALP_BREAKOUT_CONFIRM=True, SCALP_BREAKOUT_LOOKBACK=5,
    SCALP_MAX_VWAP_DISTANCE_PCT=0.12,
    SCALP_RSI_PERIOD=14, SCALP_CE_RSI_MIN=52, SCALP_CE_RSI_MAX=68, SCALP_PE_RSI_MIN=32, SCALP_PE_RSI_MAX=48,
    SCALP_USE_MACD=True,
    SCALP_VOL_MULT=1.5, SCALP_VOL_AVG_N=20, SCALP_MIN_BODY_PCT=0.03, SCALP_CLOSE_POS_MIN=0.70,
    SCALP_MAX_OPPOSITE_WICK_RATIO=0.40, SCALP_MAX_SIGNAL_ATR=1.5, SCALP_STRUCTURE_FILTER=True,
    SCALP_OPENING_RANGE_FILTER=True, SCALP_MAX_OPENING_RANGE_PCT=0.35,
    SCALP_CROSS_INDEX_FILTER=True,
    SCALP_VIX_FILTER=True, VIX_PERCENTILE_LOOKBACK=20, SCALP_VIX_LOW_PCTL=10, SCALP_VIX_HIGH_PCTL=95,
    TRADE_SCORE_MIN=75, SCALP_CE_ENABLED=True, SCALP_PE_ENABLED=True,
    SCALP_V2_STRICT=False,
)


def P(cfg, k):
    """Setting from a dict / module, falling back to DEFAULTS."""
    if isinstance(cfg, dict):
        return cfg.get(k, DEFAULTS.get(k))
    return getattr(cfg, k, DEFAULTS.get(k))


def _wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


# ---------------------------------------------------------------------------------------------- features
def features(df, cfg, vix_daily=None, other=None):
    """
    df     : 1-min index candles, DatetimeIndex (IST, candle start), columns o h l c [v]. Completed candles only.
    vix_daily: pd.Series of India VIX daily closes indexed by date (optional).
    other  : candles of the confirming index (same format, optional).
    Returns a DataFrame of features aligned to df. Row t uses only candles <= t.
    """
    f = pd.DataFrame(index=df.index)
    o, h, l, c = (df[k].astype(float) for k in ("o", "h", "l", "c"))
    v = df["v"].astype(float) if "v" in df else pd.Series(np.nan, index=df.index)
    day = pd.Series(df.index.date, index=df.index)
    f["day"] = day
    f["tod"] = df.index.hour * 60 + df.index.minute
    f["o"], f["h"], f["l"], f["c"], f["v"] = o, h, l, c, v

    # trend
    L = int(P(cfg, "SCALP_EMA_SLOPE_LOOKBACK"))
    f["ema9"], f["ema21"] = _ema(c, 9), _ema(c, 21)
    f["ema9_slope"] = f.ema9 - f.ema9.shift(L)
    f["ema21_slope"] = f.ema21 - f.ema21.shift(L)
    f["ema_sep_pct"] = (f.ema9 - f.ema21).abs() / c * 100
    sign = np.sign(f.ema9 - f.ema21)
    f["ema_crosses"] = (sign != sign.shift(1)).astype(int).rolling(20, min_periods=1).sum()

    # volatility
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = _wilder(tr, 14)
    f["atr_pct"] = f.atr / c * 100
    f["atr_pct_med"] = f.atr_pct.shift(1).rolling(375, min_periods=100).median()
    rng14 = h.rolling(14).max() - l.rolling(14).min()
    f["chop"] = 100 * np.log10(tr.rolling(14).sum() / rng14.replace(0, np.nan)) / math.log10(14)

    # ADX (Wilder)
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_w = _wilder(tr, 14)
    pdi, mdi = 100 * _wilder(pdm, 14) / atr_w, 100 * _wilder(mdm, 14) / atr_w
    f["adx"] = _wilder(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan), 14)

    # momentum
    n = int(P(cfg, "SCALP_RSI_PERIOD"))
    d = c.diff()
    ag, al = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    f["rsi"] = 100 - 100 / (1 + ag / al.replace(0, np.nan))
    f["rsi_slope"] = f.rsi - f.rsi.shift(L)
    macd = _ema(c, 12) - _ema(c, 26)
    f["macd"], f["macd_sig"] = macd, _ema(macd, 9)
    f["macd_hist"] = f.macd - f.macd_sig
    f["hist_rising"] = (f.macd_hist > f.macd_hist.shift(1)) & (f.macd_hist.shift(1) >= f.macd_hist.shift(2))
    f["hist_falling"] = (f.macd_hist < f.macd_hist.shift(1)) & (f.macd_hist.shift(1) <= f.macd_hist.shift(2))

    # VWAP (session) — volume is the NIFTYBEES proxy; falls back to an equal-weight session average
    tp = (h + l + c) / 3
    vv = v.fillna(0)
    cum_pv, cum_v = (tp * vv).groupby(day).cumsum(), vv.groupby(day).cumsum()
    f["vwap"] = (cum_pv / cum_v.replace(0, np.nan)).fillna(tp.groupby(day).expanding().mean().reset_index(level=0, drop=True))
    f["vwap_dist_pct"] = (c - f.vwap).abs() / f.vwap * 100
    an = int(P(cfg, "SCALP_VOL_AVG_N"))
    f["rvol"] = v / v.shift(1).rolling(an, min_periods=max(5, an // 2)).mean()

    # candle anatomy
    rng = (h - l).replace(0, np.nan)
    f["range"] = h - l
    f["body_pct"] = (c - o).abs() / c * 100
    f["close_pos"] = (c - l) / rng
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    f["lower_wick"] = (np.minimum(o, c) - l) / rng
    f["ext_atr"] = f["range"] / f.atr

    # market structure on 3-candle blocks: C = t-2..t, B = t-5..t-3, A = t-8..t-6
    hiC, loC = h.rolling(3).max(), l.rolling(3).min()
    hiB, loB, hiA, loA = hiC.shift(3), loC.shift(3), hiC.shift(6), loC.shift(6)
    f["struct_up_basic"] = (hiC > hiB) & (loC > loB)
    f["struct_dn_basic"] = (loC < loB) & (hiC < hiB)
    f["struct_up_full"] = f.struct_up_basic & (hiB > hiA) & (loB > loA)
    f["struct_dn_full"] = f.struct_dn_basic & (loB < loA) & (hiB < hiA)

    # entry trigger 1 — pullback: within the previous N candles price came back to EMA9 (or below) but
    # never CLOSED beyond EMA21; this candle closes beyond the pullback's extreme
    N = int(P(cfg, "SCALP_PULLBACK_LOOKBACK"))
    tol = P(cfg, "SCALP_PULLBACK_TOL_PCT") / 100
    touch_up = (l <= f.ema9 * (1 + tol)).astype(int)
    touch_dn = (h >= f.ema9 * (1 - tol)).astype(int)
    held_up = (c > f.ema21).astype(int)
    held_dn = (c < f.ema21).astype(int)
    win = lambda s, fn: getattr(s.shift(1).rolling(N), fn)()
    f["pb_high"], f["pb_low"] = win(h, "max"), win(l, "min")
    f["pullback_ce"] = (win(touch_up, "max") == 1) & (win(held_up, "min") == 1) & (c > f.pb_high) & (c > o)
    f["pullback_pe"] = (win(touch_dn, "max") == 1) & (win(held_dn, "min") == 1) & (c < f.pb_low) & (c < o)

    # entry trigger 2 — volume breakout of the previous K candles, closing near the candle's extreme
    K = int(P(cfg, "SCALP_BREAKOUT_LOOKBACK"))
    f["box_high"], f["box_low"] = h.shift(1).rolling(K).max(), l.shift(1).rolling(K).min()
    vm = P(cfg, "SCALP_VOL_MULT")
    cp = P(cfg, "SCALP_CLOSE_POS_MIN")
    f["bo_ce_raw"] = (c > f.box_high) & (f.close_pos >= cp) & (f.rvol >= vm) & (c > o)
    f["bo_pe_raw"] = (c < f.box_low) & (f.close_pos <= 1 - cp) & (f.rvol >= vm) & (c < o)
    # false-breakout guard: the NEXT candle must hold beyond the broken level and above/below the breakout
    # candle's midpoint; the entry is taken on that confirming candle (one candle later, no look-ahead)
    prev_mid = (h.shift(1) + l.shift(1)) / 2
    f["bo_ce_conf"] = f.bo_ce_raw.shift(1, fill_value=False) & (c > f.box_high.shift(1)) & (c > prev_mid) & (l > f.box_high.shift(1) - 0.25 * f.atr)
    f["bo_pe_conf"] = f.bo_pe_raw.shift(1, fill_value=False) & (c < f.box_low.shift(1)) & (c < prev_mid) & (h < f.box_low.shift(1) + 0.25 * f.atr)

    # opening range shock: block while the first 5 minutes were abnormally wide AND volatility is still
    # elevated vs the previous day's median
    first = df[(f.tod >= 555) & (f.tod < 560)]
    orng = (first.h.groupby(first.index.date).max() - first.l.groupby(first.index.date).min()) / \
        first.o.groupby(first.index.date).first() * 100
    f["or_pct"] = day.map(orng).where(f.tod >= 560)
    day_med = f.atr_pct.groupby(day).median()
    prev_med = day_med.shift(1)
    f["prev_day_atr_med"] = day.map(prev_med)
    f["or_block"] = (f.or_pct > P(cfg, "SCALP_MAX_OPENING_RANGE_PCT")) & ~(f.atr_pct <= 1.5 * f.prev_day_atr_med)

    # India VIX: previous close's percentile among the previous VIX_PERCENTILE_LOOKBACK closes
    f["vix_pctl"] = np.nan
    if vix_daily is not None and len(vix_daily):
        vs = pd.Series(vix_daily).sort_index()
        lb = int(P(cfg, "VIX_PERCENTILE_LOOKBACK"))
        pct = {}
        dates = list(vs.index)
        for dd in sorted(set(day)):
            prev = [x for x in dates if x < dd]
            if len(prev) >= max(5, lb // 2):
                window = vs.loc[prev[-lb:]]
                pct[dd] = 100 * (window <= window.iloc[-1]).mean()
        f["vix_pctl"] = day.map(pct).astype(float)

    # confirming index: +1 strongly bullish, -1 strongly bearish, 0 otherwise
    f["other_bias"] = 0
    if other is not None and len(other):
        oc = other["c"].astype(float)
        e9, e21 = _ema(oc, 9), _ema(oc, 21)
        bull = (e9 > e21) & (e9 - e9.shift(L) > 0) & (e21 - e21.shift(L) > 0) & (oc > e21)
        bear = (e9 < e21) & (e9 - e9.shift(L) < 0) & (e21 - e21.shift(L) < 0) & (oc < e21)
        bias = pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=other.index)
        f["other_bias"] = bias.reindex(df.index, method="ffill", limit=2).fillna(0).astype(int)

    # regime
    ok_vol = f.atr_pct >= P(cfg, "SCALP_ATR_MIN_PCT")
    choppy = P(cfg, "SCALP_CHOP_FILTER") & ((f.chop > P(cfg, "SCALP_CHOP_MAX")) | (f.ema_crosses >= P(cfg, "SCALP_MAX_EMA_CROSSES")))
    strong = (f.adx >= P(cfg, "SCALP_ADX_MIN")) & (f.ema_sep_pct >= P(cfg, "SCALP_EMA_SEPARATION_MIN"))
    up = (f.ema9 > f.ema21) & (f.ema9_slope > 0) & (f.ema21_slope >= 0) & (c > f.ema21)
    dn = (f.ema9 < f.ema21) & (f.ema9_slope < 0) & (f.ema21_slope <= 0) & (c < f.ema21)
    weakish = f.adx >= P(cfg, "SCALP_ADX_MIN") - 5
    hv = f.atr_pct > P(cfg, "SCALP_HIGHVOL_MULT") * f.atr_pct_med
    f["regime"] = np.select(
        [hv, ~ok_vol | choppy, up & strong, dn & strong, up & weakish, dn & weakish],
        ["HIGH_VOLATILITY", "RANGE", "STRONG_UPTREND", "STRONG_DOWNTREND", "WEAK_UPTREND", "WEAK_DOWNTREND"],
        default="RANGE")
    return f


# ---------------------------------------------------------------------------------------------- evaluate
def evaluate(f, cfg, liquidity_score=None):
    """
    Vectorised entry decision for every row of features(). Returns a DataFrame with:
      side ("CE"/"PE"/""), entry_type, score, s_<component> scores, trigger_side, reason (why a
      triggered setup was rejected; "" when accepted).
    liquidity_score: points for option liquidity (live: from the quote); None = not known yet → full
    points are given here and the caller re-scores after it sees the quote.
    """
    out = pd.DataFrame(index=f.index)
    liq = WEIGHTS["liquidity"] if liquidity_score is None else liquidity_score
    res = {}
    for side, s in (("CE", 1), ("PE", -1)):
        up = s == 1
        c, e9, e21 = f.c, f.ema9, f.ema21
        reg_strong = f.regime == ("STRONG_UPTREND" if up else "STRONG_DOWNTREND")
        reg_weak = f.regime == ("WEAK_UPTREND" if up else "WEAK_DOWNTREND")
        ema_ok = ((e9 - e21) * s > 0) & (f.ema9_slope * s > 0) & (f.ema21_slope * s >= 0) & ((c - e9) * s > 0) & ((c - e21) * s > 0)
        vwap_side = (c - f.vwap) * s > 0
        vwap_near = f.vwap_dist_pct <= P(cfg, "SCALP_MAX_VWAP_DISTANCE_PCT")
        lo, hi = (P(cfg, "SCALP_CE_RSI_MIN"), P(cfg, "SCALP_CE_RSI_MAX")) if up else (P(cfg, "SCALP_PE_RSI_MIN"), P(cfg, "SCALP_PE_RSI_MAX"))
        rsi_band = f.rsi.between(lo, hi)
        rsi_slope = f.rsi_slope * s > 0
        macd_dir = (f.macd - f.macd_sig) * s > 0
        hist_dir = f.hist_rising if up else f.hist_falling
        pull = f["pullback_ce" if up else "pullback_pe"] if P(cfg, "SCALP_PULLBACK_ENTRY") else False
        bo_conf = f["bo_ce_conf" if up else "bo_pe_conf"]
        bo_raw = f["bo_ce_raw" if up else "bo_pe_raw"]
        bo = (bo_conf if P(cfg, "SCALP_BREAKOUT_CONFIRM") else bo_raw) if P(cfg, "SCALP_BREAKOUT_ENTRY") else False
        trigger = pull | bo
        struct_basic = f["struct_up_basic" if up else "struct_dn_basic"]
        struct_full = f["struct_up_full" if up else "struct_dn_full"]
        opp_wick = f.upper_wick if up else f.lower_wick
        strict = bool(P(cfg, "SCALP_V2_STRICT"))
        candle_ok = (opp_wick <= P(cfg, "SCALP_MAX_OPPOSITE_WICK_RATIO")) & (f.ext_atr <= P(cfg, "SCALP_MAX_SIGNAL_ATR"))
        if strict:
            candle_ok = candle_ok & (f.body_pct >= P(cfg, "SCALP_MIN_BODY_PCT"))
        vix = f.vix_pctl
        vix_low = (vix < P(cfg, "SCALP_VIX_LOW_PCTL")) & (f.atr_pct < 1.5 * P(cfg, "SCALP_ATR_MIN_PCT"))  # very low VIX + low ATR
        vix_high = vix > P(cfg, "SCALP_VIX_HIGH_PCTL")
        vix_ok = ~(vix_low | vix_high) if P(cfg, "SCALP_VIX_FILTER") else pd.Series(True, index=f.index)
        other_ok = (f.other_bias != -s) if P(cfg, "SCALP_CROSS_INDEX_FILTER") else pd.Series(True, index=f.index)

        # ---- scores
        sc = pd.DataFrame(index=f.index)
        sc["trend"] = np.where(reg_strong & ema_ok, 20, np.where((reg_strong | reg_weak) & ema_ok, 10, 0))
        sc["momentum"] = 8 * struct_full.astype(int) + 4 * struct_basic.astype(int) * (~struct_full).astype(int) + \
            4 * (f.close_pos * s + (0 if up else 1) >= P(cfg, "SCALP_CLOSE_POS_MIN")).astype(int) + \
            3 * (f.body_pct >= P(cfg, "SCALP_MIN_BODY_PCT")).astype(int)
        sc["vwap"] = np.where(vwap_side & vwap_near, np.where(f.vwap_dist_pct <= P(cfg, "SCALP_MAX_VWAP_DISTANCE_PCT") / 2, 10, 6), 0)
        sc["rsi"] = 6 * rsi_band.astype(int) + 4 * rsi_slope.astype(int)
        sc["macd"] = 5 * macd_dir.astype(int) + 5 * hist_dir.astype(int) if P(cfg, "SCALP_USE_MACD") else 10
        sc["volume"] = np.where(f.rvol >= P(cfg, "SCALP_VOL_MULT"), 10, np.where(f.rvol >= 1.0, 5, 0))
        sc["breakout"] = np.where(pull | (bo_conf if P(cfg, "SCALP_BREAKOUT_ENTRY") else False), 10, np.where(bo_raw, 5, 0))
        sc["liquidity"] = liq
        sc["volatility"] = np.where(vix_ok & (f.atr_pct >= P(cfg, "SCALP_ATR_MIN_PCT")) & f.vix_pctl.notna(), 5,
                                    np.where(f.vix_pctl.isna() & (f.atr_pct >= P(cfg, "SCALP_ATR_MIN_PCT")), 3, 0))
        score = sc.sum(axis=1)

        # ---- mandatory filters, in the order a rejection is reported
        # regime: strong trend, or a weak one (if allowed) with a high score — reported as two reasons so a
        # WEAK_UPTREND rejected on score doesn't read "not uptrend"
        rf = bool(P(cfg, "SCALP_REGIME_FILTER"))
        wmin = P(cfg, "SCALP_WEAK_TREND_SCORE_MIN")
        regime_ok = reg_strong | (reg_weak & bool(P(cfg, "SCALP_ALLOW_WEAK_TREND"))) if rf else True
        weak_ok = reg_strong | (score >= wmin) if rf else True
        checks = [
            ("side disabled", pd.Series(bool(P(cfg, f"SCALP_{side}_ENABLED")), index=f.index)),
            ("opening range too wide", ~f.or_block.fillna(False) if P(cfg, "SCALP_OPENING_RANGE_FILTER") else True),
            ("regime " + ("not uptrend" if up else "not downtrend"), regime_ok),
            (f"weak {'up' if up else 'down'}trend needs score >= {wmin:g}", weak_ok),
            ("EMA alignment/slope", ema_ok),
            ("wrong side of VWAP", vwap_side),
            ("too far from VWAP", vwap_near),
            # strict = every rule mandatory (spec-literal); otherwise RSI, MACD, structure and body are
            # scored, and TRADE_SCORE_MIN enforces them together (strict leaves ~2 trades in 19 days)
            ("RSI outside band", rsi_band if strict else True),
            ("RSI slope against", rsi_slope if strict else True),
            ("MACD against", (macd_dir & hist_dir) if P(cfg, "SCALP_USE_MACD") and strict else True),
            ("no market structure", struct_basic if P(cfg, "SCALP_STRUCTURE_FILTER") and strict else True),
            ("weak/wicky/extended candle", candle_ok),
            ("other index strongly against", other_ok),
            ("VIX regime", vix_ok),
            ("score below minimum", score >= P(cfg, "TRADE_SCORE_MIN")),
        ]
        fails = [(name, ~(pd.Series(m, index=f.index) if not isinstance(m, pd.Series) else m).fillna(False).astype(bool))
                 for name, m in checks]
        reason = pd.Series(np.select([m for _, m in fails], [n for n, _ in fails], default=""), index=f.index)
        ok = trigger & (reason == "")
        etype = pd.Series(np.where(pull, "pullback", np.where(bo, "breakout", "")), index=f.index)
        res[side] = dict(ok=ok, trigger=trigger, score=score, sc=sc, reason=reason, etype=etype)

    ce, pe = res["CE"], res["PE"]
    pick_ce = ce["ok"] & (~pe["ok"] | (ce["score"] >= pe["score"]))
    pick_pe = pe["ok"] & ~pick_ce
    out["side"] = np.where(pick_ce, "CE", np.where(pick_pe, "PE", ""))
    out["trigger_side"] = np.select([pick_ce, pick_pe, ce["trigger"], pe["trigger"]], ["CE", "PE", "CE", "PE"], default="")
    for col in ("score", "reason", "etype"):
        out[col] = np.where(out.trigger_side == "CE", ce[col], np.where(out.trigger_side == "PE", pe[col], ce[col] if col == "score" else ""))
    out.rename(columns={"etype": "entry_type"}, inplace=True)
    for k in WEIGHTS:
        out["s_" + k] = np.where(out.trigger_side == "PE", pe["sc"][k], ce["sc"][k])
    out["score_ce"], out["score_pe"] = ce["score"], pe["score"]
    out["regime"] = f.regime
    return out


# ---------------------------------------------------------------------------------------------- options
RATE = 0.065


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(spot, strike, t, iv, kind):
    """Black-Scholes price and delta."""
    t = max(t, 1 / (365 * 24 * 60))
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t) * _ncdf(d2), _ncdf(d1)
    return strike * math.exp(-RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1), _ncdf(d1) - 1


def score_options(cands, cfg, budget):
    """
    cands: list of dict(strike, delta, premium, [bid, ask, volume, oi]). Picks the best option to trade.
    Hard rejects: spread above SCALP_MAX_SPREAD_PCT, premium above budget, volume/OI below the absolute
    floors, or below SCALP_MIN_REL_LIQUIDITY x the median of the candidates (relative liquidity).
    Score: delta closeness to the middle of [SCALP_DELTA_MIN, SCALP_DELTA_MAX] (40), spread (25),
    volume (15) and OI (10) vs the candidates, distance from ATM (10). Returns (best, liquidity_points 0-10, why).
    """
    g = lambda k, d: cfg.get(k, d) if isinstance(cfg, dict) else getattr(cfg, k, d)
    dmin, dmax = g("SCALP_DELTA_MIN", 0.55), g("SCALP_DELTA_MAX", 0.70)
    mid = (dmin + dmax) / 2
    vols = [c["volume"] for c in cands if c.get("volume")]
    ois = [c["oi"] for c in cands if c.get("oi")]
    mv, mo = (float(np.median(vols)) if vols else None), (float(np.median(ois)) if ois else None)
    rel = g("SCALP_MIN_REL_LIQUIDITY", 0.25)
    best, why = None, []
    for c in cands:
        d = abs(c["delta"])
        bid, ask = c.get("bid"), c.get("ask")
        spread = (ask - bid) / ((ask + bid) / 2) * 100 if bid and ask and ask >= bid else None
        c["spread_pct"] = spread
        if c["premium"] <= 0 or c["premium"] > budget:
            why.append(f"{c['strike']:g}: premium"); continue
        if spread is not None and spread > g("SCALP_MAX_SPREAD_PCT", 1.0):
            why.append(f"{c['strike']:g}: spread {spread:.2f}%"); continue
        if c.get("volume") is not None and (c["volume"] < g("SCALP_MIN_OPTION_VOLUME", 0) or (mv and c["volume"] < rel * mv)):
            why.append(f"{c['strike']:g}: thin volume"); continue
        if c.get("oi") is not None and (c["oi"] < g("SCALP_MIN_OPTION_OI", 0) or (mo and c["oi"] < rel * mo)):
            why.append(f"{c['strike']:g}: thin OI"); continue
        s = 40 * max(0.0, 1 - abs(d - mid) / 0.25) if d >= 0.45 else 0
        s += 25 * (max(0.0, 1 - spread / g("SCALP_MAX_SPREAD_PCT", 1.0)) if spread is not None else 0.5)
        s += 15 * (min(1.0, c["volume"] / mv) if c.get("volume") and mv else 0.5)
        s += 10 * (min(1.0, c["oi"] / mo) if c.get("oi") and mo else 0.5)
        s += 10 * max(0.0, 1 - c.get("itm_steps", 1) / 4)
        c["opt_score"] = s
        if dmin - 0.05 <= d <= dmax + 0.08 and (best is None or s > best["opt_score"]):
            best = c
    if best is None:
        return None, 0, "; ".join(why) or "no strike with delta near the target"
    sp = best.get("spread_pct")
    if sp is None:
        liq = 7                                       # unknown (modelled / free feed): partial credit
    else:
        liq = round(10 * max(0.0, 1 - sp / g("SCALP_MAX_SPREAD_PCT", 1.0)) * 0.6 +
                    4 * (min(1.0, best["volume"] / mv) if best.get("volume") and mv else 0.5))
    return best, int(min(10, liq)), ""


def premium_stop(atr_index, delta, cfg):
    """Dynamic premium stop: ATR of the index x |delta| x SCALP_ATR_SL_MULT, clamped to [min, max] points."""
    g = lambda k, d: cfg.get(k, d) if isinstance(cfg, dict) else getattr(cfg, k, d)
    if not g("SCALP_DYNAMIC_SL", True):
        return float(g("SCALP_SL_POINTS", 5))
    raw = atr_index * abs(delta) * g("SCALP_ATR_SL_MULT", 0.8)
    return float(min(max(raw, g("SCALP_MIN_SL_POINTS", 5)), g("SCALP_MAX_SL_POINTS", 12)))


def side_penalty(completed, side, cfg):
    """Adaptive confidence: extra score needed for a side whose recent COMPLETED trades are losing.
    completed: list of (side, net) in time order — only trades already closed, so no look-ahead."""
    g = lambda k, d: cfg.get(k, d) if isinstance(cfg, dict) else getattr(cfg, k, d)
    if not g("SCALP_SIDE_ADAPT", True):
        return 0
    last = [n for s, n in completed if s == side][-int(g("SCALP_SIDE_LOOKBACK", 8)):]
    if len(last) < int(g("SCALP_SIDE_MIN_TRADES", 4)):
        return 0
    gw, gl = sum(x for x in last if x > 0), -sum(x for x in last if x <= 0)
    pf = gw / gl if gl > 0 else 9
    return int(g("SCALP_SIDE_PENALTY", 10)) if pf < 0.8 else 0
