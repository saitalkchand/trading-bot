"""
News analyst.

Every 10 minutes it reads the last 24h of Indian-market headlines from Google News RSS
(Nifty, Sensex, RBI, FII flows, crude/rupee, global cues, macro data releases and
corporate announcements from index heavyweights), scores each headline with a
finance word list, and produces:
  * score      : -1 (very bearish) … +1 (very bullish), recent headlines weigh more
  * event_risk : True if a high-impact event is in today's news (RBI policy, Fed decision,
                 Budget, election results, CPI/GDP data…) — big gap/whipsaw risk
  * headlines  : the top items with their tags, shown on the dashboard

It acts as a VETO only: blocks CE when sentiment is strongly bearish, PE when strongly
bullish, and (optionally) all new entries on high-impact event days.
Keyword scoring is crude by design — read the headlines yourself before going live.
"""
import email.utils, json, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

from . import cached, save

QUERIES = [
    "Nifty OR Sensex stock market",
    "RBI OR inflation OR rupee India markets",
    "FII OR FPI flows India equities",
    "crude oil prices India",
    "Wall Street OR US Fed OR global markets Asia",
    "India GDP OR CPI OR WPI OR PMI OR IIP OR trade deficit data",
    "Reliance OR HDFC Bank OR ICICI Bank OR Infosys OR TCS results OR announcement",
]
KIND = [("macro", r"\b(gdp|cpi|wpi|pmi|iip|inflation|repo|rbi|fed|fomc|rupee|deficit|jobs|yield)\b"),
        ("corporate", r"\b(results?|q[1-4]|profit|revenue|earnings|order|acquisition|merger|dividend|"
                      r"buyback|announce\w*|stake|ipo)\b")]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

BULL = {"rally": 2, "rallies": 2, "surge": 2, "surges": 2, "soar": 2, "soars": 2, "jump": 1.5, "jumps": 1.5,
        "gain": 1, "gains": 1, "rise": 1, "rises": 1, "climb": 1, "climbs": 1, "record high": 2, "all-time high": 2,
        "rebound": 1.5, "rebounds": 1.5, "recovers": 1, "upgrade": 1.5, "inflows": 1.5, "buying": 1,
        "rate cut": 2, "cuts rate": 2, "beats": 1, "strong": 0.5, "optimism": 1, "bullish": 1.5, "boost": 1,
        "ceasefire": 1.5, "strengthens": 1, "firms up": 1, "upbeat": 1, "deal": 0.5, "eases": 1, "cools": 1, "positive": 0.5, "higher": 0.5, "green": 0.5}
BEAR = {"crash": 2.5, "crashes": 2.5, "plunge": 2, "plunges": 2, "tumble": 2, "tumbles": 2, "slump": 2,
        "sink": 1.5, "sinks": 1.5, "fall": 1, "falls": 1, "drop": 1, "drops": 1, "decline": 1, "declines": 1,
        "slide": 1, "slides": 1, "sell-off": 2, "selloff": 2, "bloodbath": 2.5, "outflows": 1.5, "selling": 1,
        "rate hike": 2, "hikes rate": 2, "downgrade": 1.5, "misses": 1, "weak": 0.5, "fears": 1, "fear": 1,
        "war": 1.5, "tariff": 1, "tariffs": 1, "sanctions": 1, "recession": 1.5, "inflation surges": 1.5,
        "bearish": 1.5, "weakens": 1, "weaker": 0.5, "slips": 1, "cautious": 0.5, "lower": 0.5, "red": 0.5, "volatile": 0.5, "concern": 0.5, "concerns": 1, "under pressure": 1.5, "worries": 1, "tension": 1}
# lines that usually mean the move is about crude itself, where the market effect is inverse
OIL_INVERSE = re.compile(r"\b(crude|oil|brent)\b", re.I)
EVENTS = {
    "RBI policy": r"\b(rbi|monetary policy|mpc)\b.*\b(policy|decision|meet|repo)\b",
    "US Fed decision": r"\b(fed|fomc|powell)\b.*\b(decision|meeting|rate)\b",
    "Union Budget": r"\bunion budget\b|\bbudget (day|2026|2027)\b",
    "Election results": r"\belection results?\b|\bexit polls?\b|\bcounting day\b",
    "Key data (CPI/GDP)": r"\b(cpi|gdp|inflation data|jobs report|nonfarm)\b.*\b(today|due|data|release)\b",
    "Expiry day": r"\bexpiry\b",
}


def _fetch(q):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        try:
            ts = email.utils.parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = time.time()
        items.append(dict(title=title, source=src.text if src is not None else "",
                          link=it.findtext("link") or "", ts=ts))
    return items


def score_headline(title):
    t = title.lower()
    pos = sum(w for k, w in BULL.items() if re.search(r"\b" + re.escape(k) + r"\b", t))
    neg = sum(w for k, w in BEAR.items() if re.search(r"\b" + re.escape(k) + r"\b", t))
    s = pos - neg
    is_market = re.search(r"\b(nifty|sensex|stocks?|markets?|equit\w*|shares|dalal)\b", t)
    if OIL_INVERSE.search(t) and not is_market:
        s = -s  # "oil surges" is bad for Indian equities
    return max(-3, min(3, s))


def analyse(items, block_score=0.4, now=None):
    now = now or time.time()
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen or now - it["ts"] > 24 * 3600:
            continue
        seen.add(key); uniq.append(it)
    num = den = 0.0
    events = []
    for it in uniq:
        s = score_headline(it["title"])
        it["score"] = s
        age_h = (now - it["ts"]) / 3600
        w = 1.0 if age_h < 3 else 0.6 if age_h < 8 else 0.3
        num += s * w; den += 3 * w
        for name, pat in EVENTS.items():
            if name not in events and re.search(pat, it["title"], re.I) and age_h < 18:
                events.append(name)
    score = num / den * 3 if den else 0.0          # roughly -1..+1
    score = max(-1.0, min(1.0, score))
    hard_events = [e for e in events if e != "Expiry day"]
    label = ("bullish" if score >= block_score else "bearish" if score <= -block_score
             else "mildly bullish" if score > 0.1 else "mildly bearish" if score < -0.1 else "neutral")
    top = sorted(uniq, key=lambda x: (-abs(x["score"]), -x["ts"]))[:10]
    return dict(ok=bool(uniq), score=round(score, 2), label=label, headline_count=len(uniq),
                event_risk=bool(hard_events), events=events,
                headlines=[dict(title=h["title"], source=h["source"], link=h["link"], score=h["score"],
                                kind=next((k for k, pat in KIND if re.search(pat, h["title"], re.I)), "market"),
                                age_min=int((now - h["ts"]) / 60)) for h in top],
                note=(f"{len(uniq)} headlines, sentiment {score:+.2f} ({label})"
                      + (f"; events: {', '.join(events)}" if events else "")))


def get(block_score=0.4, max_age=600):
    c = cached("news", max_age)
    if c:
        return c
    items, errs = [], []
    for q in QUERIES:
        try:
            items += _fetch(q)
        except Exception as e:
            errs.append(str(e)[:80])
    if not items:
        return save("news", dict(ok=False, score=0, label="unknown", event_risk=False, events=[], headlines=[],
                                 note="News unavailable (" + "; ".join(errs[:2]) + "). Filter skipped."))
    return save("news", analyse(items, block_score))


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=2)[:3000])
