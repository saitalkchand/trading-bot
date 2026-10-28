"""
Crude-oil agent.

India imports ~85% of its crude, so sharp oil moves tend to push Indian indices the
other way (oil up -> inflation/rupee/OMC worries -> bearish; oil down -> bullish).
The link is real but loose and works best on big moves, so the agent acts only as a
VETO on large moves (default ±1.0% vs previous close), never as a reason to trade.

Data: Brent futures (BZ=F) from Yahoo Finance (via agents.market, which handles Yahoo's
429 rate limit), falls back to WTI (CL=F). Free, ~15 min delayed. If both fail, the last
good reading is kept for up to 30 minutes.
"""
import json, time
from datetime import datetime, timezone

from . import cached, load_cache, save
from . import market

SYMBOLS = [("BZ=F", "Brent"), ("CL=F", "WTI")]


def _fetch(sym):
    d = market.chart(sym, "5m", "2d", max_age=120)
    ts, closes = d["t"], d["c"]
    price = closes[-1]
    # previous close = last price of the prior UTC session
    last_day = datetime.fromtimestamp(ts[-1], timezone.utc).date()
    prior = [c for t, c in zip(ts, closes) if datetime.fromtimestamp(t, timezone.utc).date() < last_day]
    prev = prior[-1] if prior else closes[0]
    hour_ago = closes[-13] if len(closes) > 13 else closes[0]
    return price, prev, hour_ago


def analyse(price, prev, hour_ago, name, block_pct):
    ch = (price - prev) / prev * 100
    ch1h = (price - hour_ago) / hour_ago * 100
    if ch >= block_pct:
        bias, note = "bearish", f"{name} up {ch:.2f}% — negative for Indian markets; CE (buy) trades blocked."
    elif ch <= -block_pct:
        bias, note = "bullish", f"{name} down {abs(ch):.2f}% — supportive for Indian markets; PE (sell) trades blocked."
    else:
        bias, note = "neutral", f"{name} {ch:+.2f}% — within ±{block_pct}%, no filter applied."
    return dict(ok=True, name=name, price=round(price, 2), prev_close=round(prev, 2),
                change_pct=round(ch, 2), change_1h_pct=round(ch1h, 2), bias=bias, note=note)


def get(block_pct=1.0, max_age=180):
    c = cached("crude", max_age)
    if c:
        return c
    errs = []
    for sym, name in SYMBOLS:
        try:
            return save("crude", analyse(*_fetch(sym), name, block_pct))
        except Exception as e:
            errs.append(str(e)[:120])
    prev = load_cache().get("crude") or {}
    if prev.get("ok") and time.time() - prev.get("fetched_at", 0) < 1800:
        # keep the last good reading, but don't refresh its timestamp so it ages out
        prev["note"] = prev["note"].split(" (last good")[0] + " (last good reading — refresh failed)"
        return prev
    return save("crude", dict(ok=False, change_pct=None, bias="unknown",
                              note=f"Crude data unavailable ({' | '.join(errs)}). Filter skipped."))


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=2))
