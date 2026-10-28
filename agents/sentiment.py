"""
Sentiment analyst — crowd mood and market tone.

  * Social: newest posts on r/IndianStreetBets, r/IndianStockMarket and r/IndiaInvestments
    (public RSS — Reddit's JSON API and StockTwits block scripts), scored with the news
    word list plus trader slang. Posts from the last 12 hours only.
  * Fear gauge: India VIX level and change vs the previous close (rising VIX = fear).
  * Breadth: how many of the Nifty heavyweights are up vs down today.

Retail social chatter is noisy and often contrarian at extremes, so it carries less
weight than the fear gauge and breadth.
"""
import json, re, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from . import cached, clip, load_cache, report, save
from . import market
from .fundamental import HEAVYWEIGHTS
from .news import score_headline

SUBS = ["IndianStreetBets", "IndianStockMarket", "IndiaInvestments"]
ATOM = "{http://www.w3.org/2005/Atom}"
SLANG = {r"\bcalls?\b": 0.5, r"\bmoon\b|🚀": 1, r"\bbull(s|ish)?\b": 1, r"\bbtfd\b|\bbuy the dip\b": 1,
         r"\bputs?\b": -0.5, r"\bshort(ing)?\b": -0.5, r"\bbear(s|ish)?\b": -1, r"\bcrash(ing)?\b": -1.5,
         r"\bbloodbath\b|\bred day\b": -1.5, r"\bpanic\b": -1}


def _reddit(subs):
    """One request for all subreddits (r/a+b+c) — separate requests get HTTP 429."""
    req = urllib.request.Request(f"https://www.reddit.com/r/{'+'.join(subs)}/new/.rss?limit=100", headers=market.UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    out = []
    for e in root.iter(ATOM + "entry"):
        try:
            ts = datetime.fromisoformat(e.findtext(ATOM + "updated")).timestamp()
        except Exception:
            ts = time.time()
        cat = e.find(ATOM + "category")
        out.append(dict(title=(e.findtext(ATOM + "title") or "").strip(), ts=ts,
                        sub=cat.get("term") if cat is not None else "",
                        link=(e.find(ATOM + "link").get("href") if e.find(ATOM + "link") is not None else "")))
    return out


def score_post(title):
    s = score_headline(title)
    t = title.lower()
    s += sum(w for pat, w in SLANG.items() if re.search(pat, t))
    return clip(s, -3, 3)


def social(now=None):
    now = now or time.time()
    posts, errs = [], []
    try:
        posts = [p for p in _reddit(SUBS) if now - p["ts"] < 12 * 3600]
    except Exception as e:
        errs.append(f"Reddit: {str(e)[:60]}")
    for p in posts:
        p["score"] = score_post(p["title"])
    scored = [p for p in posts if p["score"]]
    avg = sum(p["score"] for p in scored) / (3 * len(scored)) if scored else 0.0
    return posts, avg, errs


def get(max_age=900):
    c = cached("sentiment", max_age)
    if c:
        return c
    pts, extra, problems = [], {}, []

    posts, avg, errs = social()
    problems += errs
    prev = load_cache().get("sentiment") or {}
    if not posts and prev.get("social_point") and time.time() - prev.get("fetched_at", 0) < 3600:
        # Reddit throttled us: reuse the last social reading for up to an hour
        pts.append(tuple(prev["social_point"]))
        extra.update({k: prev[k] for k in ("posts", "social_score", "post_count", "social_point") if k in prev})
    elif posts:
        n_bull = sum(p["score"] > 0 for p in posts); n_bear = sum(p["score"] < 0 for p in posts)
        w = clip(avg * 0.5, -0.25, 0.25)
        point = (f"Social chatter {'upbeat' if w > 0 else 'gloomy' if w < 0 else 'mixed'}: "
                 f"{n_bull} bullish vs {n_bear} bearish posts", round(w, 2))
        pts.append(point)
        extra["social_point"] = point
        extra["posts"] = [dict(title=p["title"], sub=p["sub"], link=p["link"], score=p["score"],
                               age_min=int((time.time() - p["ts"]) / 60))
                          for p in sorted(posts, key=lambda p: (-abs(p["score"]), -p["ts"]))[:8]]
        extra["social_score"] = round(avg, 2)
        extra["post_count"] = len(posts)

    try:
        v = market.chart("^INDIAVIX", "1d", "5d", max_age=600)["c"]
        vix, ch = v[-1], (v[-1] - v[-2]) / v[-2] * 100 if len(v) > 1 else 0
        extra.update(vix=round(vix, 2), vix_change_pct=round(ch, 1))
        if ch >= 5:
            pts.append((f"India VIX up {ch:.1f}% to {vix:.1f} — fear rising", -0.35))
        elif ch <= -5:
            pts.append((f"India VIX down {abs(ch):.1f}% to {vix:.1f} — fear easing", 0.25))
        if vix >= 20:
            pts.append((f"India VIX {vix:.1f} is high — nervous market, wider swings", -0.15))
        elif vix <= 12:
            pts.append((f"India VIX {vix:.1f} is low — calm market", 0.1))
    except Exception as e:
        problems.append(f"VIX: {str(e)[:60]}")

    try:
        closes = market.closes([s for s, _ in HEAVYWEIGHTS], rng="5d")
        up = sum(1 for c in closes.values() if len(c) > 1 and c[-1] > c[-2])
        down = sum(1 for c in closes.values() if len(c) > 1 and c[-1] < c[-2])
        extra.update(breadth_up=up, breadth_down=down)
        if up + down:
            b = (up - down) / (up + down)
            pts.append((f"Breadth: {up} of {up + down} heavyweights up today", round(b * 0.3, 2)))
    except Exception as e:
        problems.append(f"breadth: {str(e)[:60]}")

    if not pts and problems:
        return save("sentiment", dict(ok=False, score=0, label="unknown", bull=[], bear=[],
                                      note="Sentiment unavailable (" + "; ".join(problems[:2]) + ")."))
    note = (f"{extra.get('post_count', 0)} social posts" + (f", VIX {extra['vix']}" if "vix" in extra else "")
            + (f", breadth {extra['breadth_up']}↑ {extra['breadth_down']}↓" if "breadth_up" in extra else ""))
    return save("sentiment", report(pts, note=note, problems=problems, **extra))


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=2)[:3000])
