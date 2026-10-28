"""
Fundamental analyst — financial health of the stocks that move the index.

Indian companies don't file 10-Ks; they file quarterly results and annual reports with
NSE/BSE. Yahoo Finance summarises those statements, so for the Nifty heavyweights
(about half the index by weight) this agent reads, via yfinance:
  * earnings growth and revenue growth (latest quarter vs a year ago)
  * forward vs trailing P/E (are analysts expecting earnings to rise?)
  * debt-to-equity (non-financials) and analyst consensus
  * whether a heavyweight reports results today (event risk)
and rolls them into one index-level score, weighted by approximate index weight.

Fundamentals move slowly and barely matter intraday, so this refreshes every 12 hours
and only nudges the debate. It never blocks a trade on its own.
"""
import json, time, warnings
from datetime import datetime
from zoneinfo import ZoneInfo

from . import cached, clip, report, save

IST = ZoneInfo("Asia/Kolkata")
# (Yahoo symbol, approximate Nifty-50 weight %) — weights drift; only the ranking matters here
HEAVYWEIGHTS = [("HDFCBANK.NS", 12), ("RELIANCE.NS", 9), ("ICICIBANK.NS", 8), ("INFY.NS", 5),
                ("BHARTIARTL.NS", 4.5), ("LT.NS", 4), ("TCS.NS", 3.5), ("ITC.NS", 3.5),
                ("SBIN.NS", 3), ("AXISBANK.NS", 3), ("KOTAKBANK.NS", 3), ("M&M.NS", 2.5)]
FINANCIALS = {"HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS"}


def stock_view(sym, i):
    """Score one company -1..+1 from its yfinance .info dict."""
    parts, why = [], []
    eg, rg = i.get("earningsGrowth"), i.get("revenueGrowth")
    if eg is not None:
        parts.append(0.4 * clip(eg / 0.2)); why.append(f"EPS {eg:+.0%}")
    if rg is not None:
        parts.append(0.2 * clip(rg / 0.15)); why.append(f"revenue {rg:+.0%}")
    tpe, fpe = i.get("trailingPE"), i.get("forwardPE")
    if tpe and fpe and tpe > 0 and fpe > 0:
        parts.append(0.15 if fpe < tpe * 0.95 else -0.15 if fpe > tpe * 1.05 else 0)
    de = i.get("debtToEquity")
    if de is not None and sym not in FINANCIALS and de > 150:
        parts.append(-0.15); why.append(f"D/E {de:.0f}%")
    rm = i.get("recommendationMean")
    if rm:
        parts.append(0.1 * clip((3 - rm) / 2))
    return clip(sum(parts)), ", ".join(why)


def get(max_age=12 * 3600):
    c = cached("fundamental", max_age)
    if c:
        return c
    import yfinance as yf
    rows, errs = [], []
    today = datetime.now(IST).date()
    for sym, wt in HEAVYWEIGHTS:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                info = yf.Ticker(sym).info
            s, why = stock_view(sym, info)
            et = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            reports_today = bool(et) and datetime.fromtimestamp(et, IST).date() == today
            rows.append(dict(symbol=sym.replace(".NS", ""), weight=wt, score=round(s, 2), detail=why,
                             pe=round(info["trailingPE"], 1) if info.get("trailingPE") else None,
                             results_today=reports_today))
        except Exception as e:
            errs.append(f"{sym}: {str(e)[:50]}")
        time.sleep(0.4)
    if not rows:
        return save("fundamental", dict(ok=False, score=0, label="unknown", bull=[], bear=[],
                                        note="Fundamentals unavailable (" + "; ".join(errs[:2]) + ")."))
    tw = sum(r["weight"] for r in rows)
    idx = sum(r["score"] * r["weight"] for r in rows) / tw
    pts = []
    good = [r["symbol"] for r in rows if r["score"] >= 0.25]
    bad = [r["symbol"] for r in rows if r["score"] <= -0.25]
    if good:
        pts.append((f"Strong earnings/health: {', '.join(good[:5])}", clip(idx, 0, 0.3) or 0.05))
    if bad:
        pts.append((f"Weak earnings/health: {', '.join(bad[:5])}", clip(idx, -0.3, 0) or -0.05))
    pes = [(r["pe"], r["weight"]) for r in rows if r["pe"]]
    pe = sum(p * w for p, w in pes) / sum(w for _, w in pes) if pes else None
    if pe and pe >= 30:
        pts.append((f"Heavyweights look expensive (weighted P/E {pe:.0f})", -0.1))
    elif pe and pe <= 18:
        pts.append((f"Heavyweights look cheap (weighted P/E {pe:.0f})", 0.1))
    events = [r["symbol"] for r in rows if r["results_today"]]
    out = report(pts, note=f"{len(rows)} heavyweights, health score {idx:+.2f}"
                 + (f"; results today: {', '.join(events)}" if events else ""),
                 stocks=sorted(rows, key=lambda r: -r["weight"]), weighted_pe=round(pe, 1) if pe else None,
                 results_today=events, problems=errs)
    out["score"] = round(clip(idx), 2)  # the index-weighted health score is the headline number
    return save("fundamental", out)


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=2)[:3000])
