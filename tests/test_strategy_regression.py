"""Strategy v2 regression: signal_v2 must give exactly the recorded signals on a fixed synthetic series.
If a change to strategy.py is intended, regenerate with:  python tests/test_strategy_regression.py --update"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import strategy

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "signal_v2.json")
CFG = {k: getattr(config, k) for k in ("EMA_FAST", "EMA_SLOW", "EMA_TREND", "CROSS_LOOKBACK", "RSI_PERIOD",
                                       "RSI_BUY_CE", "RSI_BUY_PE", "RSI_MAX", "MIN_ATR_PCT", "RR", "HTF_FILTER",
                                       "ADX_MIN")}
# pinned to the repo defaults so a Settings change doesn't break the test
CFG.update(EMA_FAST=9, EMA_SLOW=21, EMA_TREND=50, CROSS_LOOKBACK=3, RSI_PERIOD=14, RSI_BUY_CE=55, RSI_BUY_PE=45,
           RSI_MAX=75, MIN_ATR_PCT=0.05, RR=2.0, HTF_FILTER=True, ADX_MIN=0)


def candles(seed, n=900):
    rnd, p, out, drift = random.Random(seed), 25000.0, [], 0.0
    for i in range(n):
        if i % 60 == 0:
            drift = rnd.choice([-4, -2, 0, 2, 4])
        o = p
        c = o + drift + rnd.gauss(0, 12)
        h, l = max(o, c) + abs(rnd.gauss(0, 6)), min(o, c) - abs(rnd.gauss(0, 6))
        out.append(dict(o=o, h=h, l=l, c=c))
        p = c
    return out


def run():
    res = {}
    for seed in (1, 2, 3):
        cs = candles(seed)
        sigs = []
        for i in range(120, len(cs)):
            s = strategy.signal_v2(cs[:i], CFG)
            if s:
                sigs.append([i, s["side"], round(s["entry"], 2), round(s["sl"], 2), round(s["target"], 2)])
        res[str(seed)] = sigs
    return res


def test_signals_unchanged():
    with open(GOLDEN) as f:
        assert run() == json.load(f)


if __name__ == "__main__" and "--update" in sys.argv:
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w") as f:
        json.dump(run(), f, indent=1)
    print("golden updated:", {k: len(v) for k, v in run().items()})
