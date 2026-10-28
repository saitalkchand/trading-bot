"""
Technical analyst.

Reads 5-min index candles and scores RSI, MACD (12/26/9), Bollinger Bands (20, 2σ) and the
EMA-50 trend. The bot passes in its own candles when a signal fires; the panel uses Nifty
candles from Yahoo. Output is a report (see agents.report) the debate agent weighs.
It adds context to the strategy's entry rules, it doesn't replace them.
"""
import json, math

from . import cached, report, save
from . import market

import strategy


def macd(closes, fast=12, slow=26, sig=9):
    f, s = strategy.ema_series(closes, fast), strategy.ema_series(closes, slow)
    if not s:
        return None
    line = [a - b for a, b in zip(f[slow - fast:], s)]
    signal = strategy.ema_series(line, sig)
    if len(signal) < 4:
        return None
    hist = [l - g for l, g in zip(line[sig - 1:], signal)]
    return line[-1], signal[-1], hist


def bollinger(closes, n=20, k=2.0):
    w = closes[-n:]
    mid = sum(w) / n
    sd = math.sqrt(sum((x - mid) ** 2 for x in w) / n)
    return mid - k * sd, mid, mid + k * sd


def analyse(candles, name="Nifty"):
    closes = [c["c"] for c in candles]
    if len(closes) < 60:
        return dict(ok=False, score=0, label="unknown", bull=[], bear=[],
                    note=f"Not enough candles yet ({len(closes)} of 60).")
    price = closes[-1]
    pts = []

    trend = strategy.ema_series(closes, 50)
    if price > trend[-1] and trend[-1] > trend[-6]:
        pts.append(("Price above a rising EMA-50 (uptrend)", 0.3))
    elif price < trend[-1] and trend[-1] < trend[-6]:
        pts.append(("Price below a falling EMA-50 (downtrend)", -0.3))

    r = strategy.rsi(closes[-200:], 14)
    if r >= 70:
        pts.append((f"RSI {r:.0f} overbought, pullback risk", -0.2))
    elif r >= 55:
        pts.append((f"RSI {r:.0f} bullish momentum", 0.2))
    elif r <= 30:
        pts.append((f"RSI {r:.0f} oversold, bounce risk", 0.2))
    elif r <= 45:
        pts.append((f"RSI {r:.0f} bearish momentum", -0.2))

    m = macd(closes)
    if m:
        line, sig, hist = m
        crossed_up = hist[-1] > 0 and min(hist[-4:-1]) <= 0
        crossed_dn = hist[-1] < 0 and max(hist[-4:-1]) >= 0
        if crossed_up:
            pts.append(("MACD just crossed above its signal line", 0.3))
        elif crossed_dn:
            pts.append(("MACD just crossed below its signal line", -0.3))
        elif hist[-1] > 0:
            pts.append(("MACD above signal" + (", histogram rising" if hist[-1] > hist[-2] else ", but fading"),
                        0.2 if hist[-1] > hist[-2] else 0.05))
        else:
            pts.append(("MACD below signal" + (", histogram falling" if hist[-1] < hist[-2] else ", but recovering"),
                        -0.2 if hist[-1] < hist[-2] else -0.05))

    lo, mid, hi = bollinger(closes)
    pb = (price - lo) / (hi - lo) if hi > lo else 0.5
    if pb > 1:
        pts.append(("Closed above the upper Bollinger Band (stretched)", -0.15))
    elif pb < 0:
        pts.append(("Closed below the lower Bollinger Band (stretched)", 0.15))
    elif pb > 0.5:
        pts.append(("In the upper half of the Bollinger Bands", 0.1))
    else:
        pts.append(("In the lower half of the Bollinger Bands", -0.1))
    widths = [(b[2] - b[0]) / b[1] for b in (bollinger(closes[:i]) for i in range(len(closes) - 100, len(closes) + 1)
                                             if i >= 20)]
    squeeze = len(widths) > 20 and widths[-1] <= sorted(widths)[len(widths) // 5]

    return report(pts, note=f"{name} {price:,.1f} · RSI {r:.0f} · %B {pb:.2f}" + (" · Bollinger squeeze" if squeeze else ""),
                  name=name, price=round(price, 2), rsi=round(r, 1),
                  macd=round(m[0], 2) if m else None, macd_signal=round(m[1], 2) if m else None,
                  bb_lower=round(lo, 1), bb_mid=round(mid, 1), bb_upper=round(hi, 1), pct_b=round(pb, 2),
                  squeeze=squeeze)


def get(max_age=300):
    c = cached("technical", max_age)
    if c:
        return c
    try:
        d = market.chart("^NSEI", "5m", "5d", max_age=120)
        candles = [dict(o=o, h=h, l=l, c=c) for o, h, l, c in zip(d["o"], d["h"], d["l"], d["c"])]
        return save("technical", analyse(candles))
    except Exception as e:
        return save("technical", dict(ok=False, score=0, label="unknown", bull=[], bear=[],
                                      note=f"Index data unavailable ({str(e)[:120]})."))


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=2))
