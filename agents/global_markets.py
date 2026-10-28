"""
Global markets analyst — what the rest of the world is doing before and during the Indian session.

  * GIFT Nifty (NSE IX, Gift City): Nifty futures that trade 06:30–15:40 and 16:35–02:45 IST, so it
    shows where Nifty is likely to open. Its gap to the Nifty spot is the clearest overnight signal.
  * US (S&P 500, Nasdaq, Dow): the last completed session, plus S&P / Nasdaq futures, which trade
    almost round the clock and move during Indian hours.
  * Asia (Nikkei, Hang Seng, Shanghai, KOSPI, Taiwan, Straits Times): trade at the same time as India.
  * Europe (FTSE, DAX, CAC): open at 12:30–13:30 IST, so they matter for the afternoon.
  * Macro: dollar index and USD/INR (a stronger dollar / weaker rupee pulls foreign money out of India),
    US 10-year yield, gold.

Data: Moneycontrol's global-indices feed (one request, has GIFT Nifty and a live OPEN/CLOSED state that
knows about holidays) with Yahoo Finance as the fallback for everything but GIFT Nifty; futures and macro
from Yahoo. Market hours below are the regular cash sessions; status is worked out from them in each
market's own time zone, so daylight saving is handled. Holidays are only known when Moneycontrol answers.

Like the other analysts it only feeds the debate and can veto a side on a strong reading
(GLOBAL_BLOCK_SCORE); it never starts a trade.
"""
import json, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import cached, clip, load_cache, report, save
from . import market

IST = ZoneInfo("Asia/Kolkata")
MC_URL = ("https://priceapi.moneycontrol.com/technicalCompanyData/globalMarket/"
          "getGlobalIndicesListingData?view=overview&deviceType=W")

# key, name, region, time zone, sessions [(open, close), ...] local time, Yahoo symbol, Moneycontrol name
# (a lunch break is the gap between two sessions)
MARKETS = [
    ("gift", "GIFT Nifty", "India", "Asia/Kolkata", [("06:30", "15:40"), ("16:35", "02:45")], None, "GIFT NIFTY"),
    ("nifty", "Nifty 50", "India", "Asia/Kolkata", [("09:15", "15:30")], "^NSEI", None),
    ("sensex", "Sensex", "India", "Asia/Kolkata", [("09:15", "15:30")], "^BSESN", None),
    ("nikkei", "Nikkei 225", "Asia", "Asia/Tokyo", [("09:00", "11:30"), ("12:30", "15:30")], "^N225", "Nikkei 225"),
    ("kospi", "KOSPI", "Asia", "Asia/Seoul", [("09:00", "15:30")], "^KS11", "KOSPI"),
    ("taiwan", "Taiwan", "Asia", "Asia/Taipei", [("09:00", "13:30")], "^TWII", "Taiwan Weighted"),
    ("hangseng", "Hang Seng", "Asia", "Asia/Hong_Kong", [("09:30", "12:00"), ("13:00", "16:00")], "^HSI", "Hang Seng"),
    ("shanghai", "Shanghai", "Asia", "Asia/Shanghai", [("09:30", "11:30"), ("13:00", "15:00")], "000001.SS", "Shanghai Composite"),
    ("sti", "Straits Times", "Asia", "Asia/Singapore", [("09:00", "12:00"), ("13:00", "17:00")], "^STI", "Straits Times"),
    ("ftse", "FTSE 100", "Europe", "Europe/London", [("08:00", "16:30")], "^FTSE", "FTSE"),
    ("dax", "DAX", "Europe", "Europe/Berlin", [("09:00", "17:30")], "^GDAXI", "DAX"),
    ("cac", "CAC 40", "Europe", "Europe/Paris", [("09:00", "17:30")], "^FCHI", "CAC"),
    ("sp500", "S&P 500", "US", "America/New_York", [("09:30", "16:00")], "^GSPC", "S&P 500"),
    ("nasdaq", "Nasdaq", "US", "America/New_York", [("09:30", "16:00")], "^IXIC", "Nasdaq"),
    ("dow", "Dow Jones", "US", "America/New_York", [("09:30", "16:00")], "^DJI", None),
]
# key, name, Yahoo symbol, what a rise means for India
MACRO = [
    ("es", "S&P futures", "ES=F", "risk-on"),
    ("nq", "Nasdaq futures", "NQ=F", "risk-on"),
    ("dxy", "Dollar index", "DX-Y.NYB", "dollar strength pulls money out of India"),
    ("usdinr", "USD/INR", "INR=X", "weaker rupee"),
    ("us10y", "US 10Y yield", "^TNX", "higher yields pull money out of emerging markets"),
    ("gold", "Gold", "GC=F", "safe-haven buying"),
]


# ---------------------------------------------------------------- market hours
def _hm(s):
    h, m = map(int, s.split(":"))
    return h, m


def _windows(tz, sessions, day):
    """[(open_dt, close_dt), ...] for the trading day starting on `day` (a date in the market's zone)."""
    out = []
    for o, c in sessions:
        od = datetime(day.year, day.month, day.day, *_hm(o), tzinfo=tz)
        cd = datetime(day.year, day.month, day.day, *_hm(c), tzinfo=tz)
        if cd <= od:  # runs past midnight (GIFT Nifty's night session)
            cd += timedelta(days=1)
        out.append((od, cd))
    return out


def _fmt_wait(sec):
    m = int(sec // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def session_status(tz_name, sessions, now=None):
    """Open/closed state from regular hours. Weekend = Saturday/Sunday local (a Friday night session
    that runs past midnight still counts). Returns dict(status, detail, opens_ist, closes_ist, local)."""
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(IST)).astimezone(tz)
    status, detail = "closed", ""
    for back in (1, 0):  # yesterday's overnight session first, then today's
        day = (now - timedelta(days=back)).date()
        if day.weekday() >= 5:
            continue
        w = _windows(tz, sessions, day)
        for i, (o, c) in enumerate(w):
            if o <= now < c:
                status, detail = "open", "closes in " + _fmt_wait((c - now).total_seconds())
            elif i and w[i - 1][1] <= now < o:
                status, detail = "break", "lunch break, reopens in " + _fmt_wait((o - now).total_seconds())
    if status == "closed":
        for ahead in range(0, 8):
            day = (now + timedelta(days=ahead)).date()
            if day.weekday() >= 5:
                continue
            nxt = [o for o, _ in _windows(tz, sessions, day) if o > now]
            if nxt:
                detail = "opens in " + _fmt_wait((nxt[0] - now).total_seconds())
                break
        if now.weekday() >= 5:
            detail = "weekend · " + detail
    first = _windows(tz, sessions, now.date())
    ist = lambda d: d.astimezone(IST).strftime("%H:%M")
    return dict(status=status, detail=detail,
                opens_ist=ist(first[0][0]), closes_ist=ist(first[-1][1]),
                hours_ist=" · ".join(f"{ist(o)}–{ist(c)}" for o, c in first),
                local=" · ".join(f"{o}–{c}" for o, c in sessions) + " " + now.strftime("%Z"))


def sessions(now=None):
    """Market-hours table only — no network, cheap enough to rebuild on every panel poll."""
    return {k: dict(key=k, name=n, region=r, **session_status(tz, ss, now)) for k, n, r, tz, ss, _, _ in MARKETS}


# ---------------------------------------------------------------- prices
def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


_mc = {"rows": None, "at": 0}   # in-memory: the panel polls this every few seconds


def moneycontrol(max_age=30):
    """{name: dict(price, change, change_pct, prev_close, state, updated)} from one Moneycontrol request."""
    if _mc["rows"] and time.time() - _mc["at"] < max_age:
        return _mc["rows"]
    req = urllib.request.Request(MC_URL, headers=market.UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.load(r)
    cols = [h["name"] for h in j["header"]]
    rows = {}
    for grp in j.get("dataList") or []:
        for row in grp.get("data") or []:
            d = dict(zip(cols, row))
            price = _num(d.get("price"))
            if price is None:
                continue
            ts = _num(d.get("last_updated"))
            rows[d["name"]] = dict(price=price, change=_num(d.get("net_change")), change_pct=_num(d.get("percent_change")),
                                   prev_close=_num(d.get("prev_close")), state=(d.get("state") or "").upper(),
                                   high=_num(d.get("high")), low=_num(d.get("low")),
                                   updated=ts / 1000 if ts else None)
    if not rows:
        raise ValueError("Moneycontrol: no rows")
    _mc.update(rows=rows, at=time.time())
    return rows


def _yahoo(symbols):
    """{sym: dict(price, prev_close, change, change_pct)} from daily closes (one batched request)."""
    out = {}
    for s, cl in market.closes(symbols, rng="5d", max_age=240).items():
        if len(cl) >= 2:
            out[s] = dict(price=cl[-1], prev_close=cl[-2], change=cl[-1] - cl[-2],
                          change_pct=(cl[-1] - cl[-2]) / cl[-2] * 100)
    return out


def _rnd(d):
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def fetch(now=None):
    """Prices + hours for every market and macro gauge. Returns (markets, macro, problems)."""
    problems, mc = [], {}
    try:
        mc = moneycontrol()
    except Exception as e:
        problems.append(f"Moneycontrol: {str(e)[:60]}")
    yh = {}
    need = [y for _, _, _, _, _, y, m in MARKETS if y and (m not in mc)] + [s for _, _, s, _ in MACRO]
    try:
        yh = _yahoo(need)
    except Exception as e:
        problems.append(f"Yahoo: {str(e)[:60]}")
    hours = sessions(now)
    markets = []
    for k, name, region, tz, ss, ysym, mcname in MARKETS:
        row = dict(hours[k])
        q, src = (mc.get(mcname), "Moneycontrol") if mcname in mc else (yh.get(ysym), "Yahoo")
        if q:
            row.update(_rnd(q), source=src)
            # Moneycontrol knows about holidays; trust its CLOSED over our clock
            if src == "Moneycontrol" and q.get("state") == "CLOSED" and row["status"] == "open":
                row.update(status="closed", detail="closed today (holiday)")
        markets.append(row)
    macro = [dict(key=k, name=n, why=w, **_rnd(yh[s])) for k, n, s, w in MACRO if s in yh]
    return markets, macro, problems


# ---------------------------------------------------------------- the read for India
def _avg(rows, region):
    v = [r["change_pct"] for r in rows if r["region"] == region and r.get("change_pct") is not None]
    return sum(v) / len(v) if v else None


def analyse(markets, macro, nifty_spot=None):
    """Score -1 (bearish for Indian indices) … +1 (bullish). Weights reflect how directly each
    feeds the Indian session: GIFT Nifty most, then the US close and Asia, then Europe and macro."""
    by = {r["key"]: r for r in markets}
    mac = {r["key"]: r for r in macro}
    pts, extra = [], {}
    g = by.get("gift") or {}
    if g.get("change_pct") is not None:
        ch = g["change_pct"]
        txt = f"GIFT Nifty {ch:+.2f}% at {g['price']:,.0f}"
        if nifty_spot:
            prem = g["price"] - nifty_spot
            extra["gift_premium"] = round(prem, 1)
            txt += f" ({prem:+.0f} pts vs Nifty spot)"
        pts.append((txt, clip(ch / 0.8 * 0.35, -0.35, 0.35)))
    us = [by[k]["change_pct"] for k in ("sp500", "nasdaq") if by.get(k, {}).get("change_pct") is not None]
    if us:
        a = sum(us) / len(us)
        pts.append((f"US stocks {a:+.2f}% last session (S&P 500 / Nasdaq)", clip(a / 1.0 * 0.25, -0.25, 0.25)))
    fut = [mac[k]["change_pct"] for k in ("es", "nq") if k in mac]
    if fut:
        a = sum(fut) / len(fut)
        pts.append((f"US futures {a:+.2f}% now", clip(a / 0.5 * 0.15, -0.15, 0.15)))
    for region, w, scale in (("Asia", 0.2, 1.0), ("Europe", 0.15, 1.0)):
        a = _avg(markets, region)
        if a is not None:
            ups = sum(1 for r in markets if r["region"] == region and (r.get("change_pct") or 0) > 0)
            n = sum(1 for r in markets if r["region"] == region and r.get("change_pct") is not None)
            pts.append((f"{region} {a:+.2f}% on average ({ups} of {n} up)", clip(a / scale * w, -w, w)))
    if "dxy" in mac:
        ch = mac["dxy"]["change_pct"]
        pts.append((f"Dollar index {ch:+.2f}%", clip(-ch / 0.5 * 0.1, -0.1, 0.1)))
    if "usdinr" in mac:
        ch = mac["usdinr"]["change_pct"]
        pts.append((f"Rupee {'weaker' if ch > 0 else 'stronger'} ({mac['usdinr']['price']:.2f} per $, {ch:+.2f}%)",
                    clip(-ch / 0.3 * 0.1, -0.1, 0.1)))
    if "us10y" in mac:
        bps = mac["us10y"]["change"] * 100  # ^TNX is quoted in percent
        pts.append((f"US 10-year yield {bps:+.0f} bp to {mac['us10y']['price']:.2f}%", clip(-bps / 10 * 0.1, -0.1, 0.1)))
    pts = [(t, round(w, 2)) for t, w in pts if abs(w) >= 0.01]
    open_now = [r["name"] for r in markets if r["status"] == "open" and r["region"] != "India"]
    tone = sum(w for _, w in pts)
    note = ("Risk-on" if tone >= 0.25 else "Risk-off" if tone <= -0.25 else "Mixed") + " globally"
    if g.get("change_pct") is not None:
        note += f"; GIFT Nifty {g['change_pct']:+.2f}%"
    note += "; open now: " + (", ".join(open_now[:5]) if open_now else "none")
    return report(pts, note=note, markets=markets, macro=macro, open_now=open_now, **extra)


def get(max_age=170):
    c = cached("global", max_age)
    if c:
        return c
    markets, macro, problems = fetch()
    if not any(r.get("change_pct") is not None for r in markets):
        prev = load_cache().get("global") or {}
        if prev.get("ok") and time.time() - prev.get("fetched_at", 0) < 1800:
            prev["note"] = prev["note"].split(" (last good")[0] + " (last good reading — refresh failed)"
            return prev
        return save("global", dict(ok=False, score=0, label="unknown", bull=[], bear=[], markets=markets, macro=[],
                                   note="Global markets unavailable (" + "; ".join(problems[:2]) + ")."))
    spot = next((r.get("price") for r in markets if r["key"] == "nifty"), None)
    return save("global", dict(analyse(markets, macro, spot), problems=problems))


if __name__ == "__main__":
    print(json.dumps(get(max_age=0), indent=1)[:4000])
