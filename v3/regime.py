"""
Market regime per bar, from several signals at once (never a single indicator):
trend strength (ADX), trend slope (EMA-50 in ATRs), price vs EMA-50 and VWAP, higher-timeframe
direction, volatility rank (ATR %), band-width rank (compression/expansion), range breaks.

Priority when several apply: HIGH_VOLATILITY > BREAKOUT > REVERSAL > STRONG_* > SIDEWAYS
> LOW_VOLATILITY > WEAK_*.
"""
import numpy as np
import pandas as pd

REGIMES = ["STRONG_BULL", "WEAK_BULL", "STRONG_BEAR", "WEAK_BEAR", "SIDEWAYS",
           "HIGH_VOLATILITY", "LOW_VOLATILITY", "BREAKOUT", "REVERSAL"]


def sideways_votes(f):
    """0-5 votes for a sideways market: weak ADX, flat EMA-50, compressed bands, hugging VWAP, tight range."""
    return ((f.adx < 20).astype(int) + (f.slope50.abs() < 0.2).astype(int) + (f.bbw_rank < 0.35).astype(int)
            + (f.vwap_dist.abs() < 0.6).astype(int) + (f.range12 < 3.0).astype(int))


def classify(f):
    c = f.c
    up_votes = ((f.slope50 > 0.15).astype(int) + (c > f.ema50).astype(int) + (f.htf_dir > 0).astype(int)
                + (c > f.vwap).astype(int) + (f.pdi > f.mdi).astype(int))
    dn_votes = ((f.slope50 < -0.15).astype(int) + (c < f.ema50).astype(int) + (f.htf_dir < 0).astype(int)
                + (c < f.vwap).astype(int) + (f.mdi > f.pdi).astype(int))
    strong = f.adx >= 25
    prior_hi = f.h.shift(1).rolling(12).max()
    prior_lo = f.l.shift(1).rolling(12).min()
    was_tight = f.bbw_rank.shift(1).rolling(12).min() < 0.25
    breakout = was_tight & ((c > prior_hi) | (c < prior_lo)) & (f.bar_range_atr > 1.2)
    reversal = (((f.slope50 < -0.2) & (c > f.vwap) & (f.ema9 > f.ema21) & (f.rsi > 55))
                | ((f.slope50 > 0.2) & (c < f.vwap) & (f.ema9 < f.ema21) & (f.rsi < 45)))
    high_vol = (f.atr_rank >= 0.9) | (f.bar_range_atr >= 2.5)
    low_vol = f.atr_rank <= 0.15
    side = sideways_votes(f) >= 3

    lab = np.where(up_votes >= 3, "WEAK_BULL", np.where(dn_votes >= 3, "WEAK_BEAR", "SIDEWAYS"))
    lab = np.where(low_vol & (lab == "SIDEWAYS"), "LOW_VOLATILITY", lab)
    lab = np.where(side, "SIDEWAYS", lab)
    lab = np.where(strong & (up_votes >= 4), "STRONG_BULL", lab)
    lab = np.where(strong & (dn_votes >= 4), "STRONG_BEAR", lab)
    lab = np.where(reversal, "REVERSAL", lab)
    lab = np.where(breakout, "BREAKOUT", lab)
    lab = np.where(high_vol, "HIGH_VOLATILITY", lab)
    return pd.Series(lab, index=f.index)
