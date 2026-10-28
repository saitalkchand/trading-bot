"""
News / event-risk layer — separate from the signal engine, never predicts direction.

    classify(headlines, day) -> dict(level=LOW|MEDIUM|HIGH, reasons=[...])

HIGH   : scheduled high-impact event on the day (RBI policy decision, Union Budget, election
         results/counting), a Fed decision overnight, or a major geopolitical shock (war, strikes)
MEDIUM : key data due (CPI/inflation, GDP, jobs), geopolitical tension, tariff/sanction news,
         a Fed decision later the same day
LOW    : nothing scheduled or unusual

Scheduled events come from v3/events.csv (RBI / Fed / Budget / results dates are published in
advance, so a calendar is not look-ahead). The dates in it for the research period were confirmed
from the news archive; for live use fill it from the official RBI and FOMC calendars ahead of time.
Headlines then add only UNSCHEDULED risk: a surge in geopolitical coverage (≥3× the previous days
and ≥8 headlines → HIGH; ≥2× and ≥5 → MEDIUM) and macro-data previews (MEDIUM).

Policy (engine): LOW → normal · MEDIUM → higher minimum score and smaller size · HIGH → no new trades
(until NEWS_HIGH_UNTIL, default all day).

Sources
  live     : agents/news.py headlines (real-time timestamps)
  backtest : Google News headlines published on the PREVIOUS calendar days only. Google's archive
             gives dates without times, so same-day headlines could describe things that happened
             after the trade — using them would be look-ahead. Previews ("ahead of RBI policy
             tomorrow") still flag scheduled events without peeking.
"""
import email.utils, json, os, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "news")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

HIGH = {
    "RBI policy": r"\b(rbi|mpc)\b.*\b(policy|decision|meet(ing)?|repo|rate)\b|\brepo rate\b",
    "Union Budget": r"\bunion budget\b|\bbudget (day|speech)\b",
    "Election results": r"\belection results?\b|\bcounting day\b|\bexit polls?\b",
    "Fed decision": r"\b(fed|fomc|powell)\b.*\b(decision|meeting|rate (cut|hike)|policy)\b",
    "Geopolitical shock": r"\b(war|missile|airstrikes?|attack|invasion|military strike)\b",
}
MEDIUM = {
    "Inflation / CPI data": r"\b(cpi|wpi|inflation data|retail inflation)\b",
    "GDP / jobs data": r"\b(gdp|nonfarm|jobs report|payrolls|iip)\b",
    "Tariffs / sanctions": r"\b(tariffs?|sanctions?|trade war)\b",
    "Geopolitical tension": r"\b(tension|conflict|border|ceasefire)\b",
}
FORWARD = r"\b(ahead of|today|tomorrow|this week|due|awaits?|eyes|before|later)\b"


def classify(headlines, day=None, geo_baseline=None):
    """headlines: titles already restricted to what was known before the trade.
    geo_baseline: average daily count of geopolitical headlines over the previous days — an
    ONGOING conflict is background (MEDIUM); only a surge above it counts as a shock (HIGH)."""
    hits_h, hits_m = {}, {}
    geo = 0
    for t in headlines:
        tl = t.lower()
        for name, pat in HIGH.items():
            if re.search(pat, tl):
                if name == "Geopolitical shock":
                    geo += 1
                elif re.search(FORWARD, tl):          # scheduled events count when previewed
                    hits_h[name] = hits_h.get(name, 0) + 1
                else:
                    hits_m[name] = hits_m.get(name, 0) + 1
        for name, pat in MEDIUM.items():
            if re.search(pat, tl):
                hits_m[name] = hits_m.get(name, 0) + 1
    high = [k for k, v in hits_h.items() if v >= 2]          # one stray headline isn't enough
    base = max(geo_baseline or 0, 1)
    if geo >= 8 and geo >= 3 * base:
        high.append("Geopolitical shock (surge)")
    elif geo >= 5 and geo >= 2 * base:
        hits_m["Geopolitical tension rising"] = geo
    med = [k for k, v in hits_m.items() if v >= 2] + [k for k, v in hits_h.items() if v == 1]
    level = "HIGH" if high else "MEDIUM" if med else "LOW"
    return dict(level=level, reasons=high or med, geo=geo)


# Google ignores the date operators when the query is long — keep each query short,
# and reject any answer that contains items outside the requested window.
QUERIES = ['(RBI OR repo OR Fed OR FOMC OR CPI OR inflation OR GDP OR budget OR election) (Sensex OR Nifty)',
           '(war OR missile OR attack OR tariff OR sanctions) (Sensex OR Nifty)']


def _fetch_day(d):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{d}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    out = []
    for q in QUERIES:
        q = f"{q} after:{d - timedelta(days=1)} before:{d + timedelta(days=1)}"
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-IN&gl=IN&ceid=IN:en"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            root = ET.fromstring(r.read())
        dates = []
        for it in root.iter("item"):
            try:
                pd_ = email.utils.parsedate_to_datetime(it.findtext("pubDate")).date()
            except Exception:
                continue
            dates.append(pd_)
            if pd_ == d:
                out.append(it.findtext("title") or "")
        if dates and max(dates) > d + timedelta(days=1):
            raise RuntimeError(f"date filter ignored for {d} — refusing look-ahead data")
        time.sleep(1.0)
    with open(path, "w") as f:
        json.dump(out, f)
    return out


def _geo_count(titles):
    return sum(1 for t in titles if re.search(HIGH["Geopolitical shock"], t.lower()))


def history(days, lookback=1, baseline_days=5):
    """{trade_day: classify(headlines from the `lookback` calendar days before it)}. Cached.
    The geopolitical baseline uses the `baseline_days` calendar days before those."""
    res = {}
    for d in days:
        titles, errs, base = [], 0, []
        for k in range(1, lookback + 1):
            try:
                titles += _fetch_day(d - timedelta(days=k))
            except Exception:
                errs += 1
        for k in range(lookback + 1, lookback + 1 + baseline_days):
            try:
                base.append(_geo_count(_fetch_day(d - timedelta(days=k))))
            except Exception:
                pass
        r = classify(titles, d, geo_baseline=sum(base) / len(base) if base else None)
        r["headlines"] = len(titles)
        r["complete"] = errs == 0
        res[d] = r
    return res


def calendar(path=os.path.join(HERE, "events.csv")):
    import csv
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {date.fromisoformat(r["date"]): (r["level"], r["event"]) for r in csv.DictReader(f)}


def levels(days, lookback=1):
    """Final day → LOW/MEDIUM/HIGH: calendar events (HIGH on the day, MEDIUM the day before),
    plus headline-based unscheduled risk. Scheduled-event previews in headlines are left to the
    calendar, so a 'this week' preview no longer flags every day of the week."""
    cal = calendar()
    hist = history(days, lookback)
    out = {}
    for d in days:
        h = hist[d]
        reasons = [r for r in h["reasons"] if r.startswith("Geopolitical") or r in MEDIUM or r == "Ongoing geopolitical risk"]
        lvl = "HIGH" if "Geopolitical shock (surge)" in reasons else "MEDIUM" if reasons else "LOW"
        if d in cal:
            lvl = "HIGH"; reasons = [cal[d][1]] + reasons
        elif d + timedelta(days=1) in cal and lvl == "LOW":
            lvl = "MEDIUM"; reasons = [f"day before {cal[d + timedelta(days=1)][1]}"]
        out[d] = dict(level=lvl, reasons=reasons)
    return out


def live(cache_headlines, geo_baseline=None):
    """Live use: headlines from agents/news.py (real timestamps, all before 'now')."""
    return classify([h["title"] for h in cache_headlines or []], geo_baseline=geo_baseline)
