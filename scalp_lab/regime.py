"""
Market regime from completed-candle features, and the no-trade filter built on it.

Regimes: TREND_UP, TREND_DOWN, RANGE, HIGH_VOL, LOW_ACTIVITY, UNRELIABLE (data), MIXED.
Thresholds below are a-priori starting values, not fitted; the lab reports how each one changes results
(sensitivity) before any of them is trusted.
"""
REGIME_DEFAULTS = dict(
    REG_FLAT_VWAP_PCT=0.02,      # |VWAP change over 10 candles| below this % of price = flat VWAP
    REG_MAX_VWAP_CROSSES=4,      # ... and this many closes across VWAP in 20 candles = chop
    REG_MIN_ATR_PCT=0.02,        # 1-min ATR below this % of price = low activity (same as SCALP_MIN_ATR_PCT)
    REG_LOW_EXPANSION=0.6,       # ATR vs its 60-candle average below this = activity dried up
    REG_HIGH_EXPANSION=1.8,      # ... above this = high-volatility burst
    REG_TREND_SLOPE_PCT=0.02,    # EMA-21 slope over 5 candles for a trend
    REG_BLOCK=("RANGE", "LOW_ACTIVITY", "UNRELIABLE"),
    REG_REQUIRE_ALIGNED=False,   # also block CE in TREND_DOWN / PE in TREND_UP
)


def classify(F, cfg=None):
    g = lambda k: (cfg or {}).get(k, REGIME_DEFAULTS[k])
    if not F.data_ok:
        return "UNRELIABLE", F.data_issue
    if F.bar.get("v") is None:
        return "UNRELIABLE", "volume proxy missing"
    price = F.bar["c"]
    atr_pct = F.atr / price * 100
    exp = F.atr_ratio()
    slope = F.vwap_slope_pct()
    crosses = F.vwap_crosses()
    if atr_pct < g("REG_MIN_ATR_PCT") or (exp is not None and exp < g("REG_LOW_EXPANSION")):
        return "LOW_ACTIVITY", f"ATR {atr_pct:.3f}% · expansion {exp or 0:.2f}"
    if exp is not None and exp > g("REG_HIGH_EXPANSION"):
        return "HIGH_VOL", f"ATR expansion {exp:.2f}x"
    if slope is not None and abs(slope) < g("REG_FLAT_VWAP_PCT") and crosses >= g("REG_MAX_VWAP_CROSSES"):
        return "RANGE", f"flat VWAP ({slope:+.3f}%), {crosses} crosses in 20 candles"
    es = F.ema_slope_pct()
    e9, e21, e50 = F.ema[9], F.ema[21], F.ema[50]
    if es is not None and es > g("REG_TREND_SLOPE_PCT") and e9 > e21 > e50:
        return "TREND_UP", f"EMA stack up, slope {es:+.3f}%"
    if es is not None and es < -g("REG_TREND_SLOPE_PCT") and e9 < e21 < e50:
        return "TREND_DOWN", f"EMA stack down, slope {es:+.3f}%"
    return "MIXED", f"VWAP crosses {crosses}, EMA-21 slope {es or 0:+.3f}%"


def no_trade(F, side, cfg=None):
    """Reason to refuse a new entry, or None."""
    g = lambda k: (cfg or {}).get(k, REGIME_DEFAULTS[k])
    reg, detail = classify(F, cfg)
    if reg in g("REG_BLOCK"):
        return f"regime {reg}: {detail}"
    if g("REG_REQUIRE_ALIGNED") and ((side == "CE" and reg == "TREND_DOWN") or (side == "PE" and reg == "TREND_UP")):
        return f"against the {reg} regime"
    return None
