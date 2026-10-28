"""
Yahoo Finance access shared by the free paper feed and the agents.

Yahoo now answers plain chart-URL requests with HTTP 429 (Too Many Requests) unless the
request carries its session cookie. So every call goes through yfinance first (it handles
the cookie), then falls back to the plain URL on both Yahoo hosts. Results are cached, and
after a 429 we back off instead of retrying straight away, which only extends the block.
If everything fails, the last good result is returned (marked stale) when it is recent enough.
"""
import json, threading, time, urllib.request, warnings

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
BACKOFF = 90          # seconds to leave a source alone after a 429
STALE_OK = 1800       # serve the last good result for up to 30 min when Yahoo is down

_lock = threading.Lock()
_cache = {}           # (sym, interval, rng) -> (fetched_at, data)
_blocked = {}         # source -> unblock time


class Unavailable(Exception):
    pass


def _rate_limited(e):
    return "429" in str(e) or "Too Many Requests" in str(e) or type(e).__name__ == "YFRateLimitError"


def _via_yfinance(sym, interval, rng):
    import yfinance as yf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.Ticker(sym).history(period=rng, interval=interval, auto_adjust=False, raise_errors=True)
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise ValueError("no rows")
    return dict(t=[int(x.timestamp()) for x in df.index], o=[float(v) for v in df["Open"]],
                h=[float(v) for v in df["High"]], l=[float(v) for v in df["Low"]],
                c=[float(v) for v in df["Close"]], v=[float(v or 0) for v in df["Volume"]])


def _via_url(host, sym, interval, rng):
    url = f"https://{host}/v8/finance/chart/{sym}?interval={interval}&range={rng}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        j = json.load(r)["chart"]["result"][0]
    q = j["indicators"]["quote"][0]
    out = dict(t=[], o=[], h=[], l=[], c=[], v=[])
    for i, ts in enumerate(j.get("timestamp") or []):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        out["t"].append(ts)
        for k, src in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            out[k].append(float(q[src][i] or 0))
    if not out["c"]:
        raise ValueError("no rows")
    if j["meta"].get("regularMarketPrice"):
        out["c"][-1] = float(j["meta"]["regularMarketPrice"])
    return out


def chart(sym, interval="5m", rng="2d", max_age=60):
    """OHLCV for a Yahoo symbol as dict(t, o, h, l, c, v) lists, oldest first.
    Adds stale=True when it had to fall back to an older cached copy."""
    key = (sym, interval, rng)
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    sources = [("yfinance", lambda: _via_yfinance(sym, interval, rng))]
    sources += [(h, (lambda h=h: _via_url(h, sym, interval, rng))) for h in HOSTS]
    errs = []
    for name, fn in sources:
        if time.time() < _blocked.get(name, 0):
            errs.append(f"{name}: backing off after 429")
            continue
        try:
            data = fn()
            with _lock:
                _cache[key] = (time.time(), data)
            return data
        except Exception as e:
            if _rate_limited(e):
                _blocked[name] = time.time() + BACKOFF
            errs.append(f"{name}: {str(e)[:70]}")
    if hit and time.time() - hit[0] < STALE_OK:
        return dict(hit[1], stale=True)
    raise Unavailable(f"{sym} — " + "; ".join(errs))


def last(sym, max_age=15):
    """Latest price (1-minute data)."""
    return chart(sym, "1m", "1d", max_age)["c"][-1]


def closes(symbols, rng="5d", max_age=300):
    """Daily closes for many symbols in one Yahoo request: {sym: [close, ...]}."""
    key = ("closes", tuple(symbols), rng)
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    if time.time() < _blocked.get("yfinance", 0):
        if hit:
            return hit[1]
        raise Unavailable("yfinance: backing off after 429")
    import yfinance as yf
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(list(symbols), period=rng, interval="1d", progress=False, auto_adjust=False,
                             threads=False)["Close"]
    except Exception as e:
        if _rate_limited(e):
            _blocked["yfinance"] = time.time() + BACKOFF
        if hit:
            return hit[1]
        raise
    out = {s: [float(x) for x in df[s].dropna()] for s in symbols if s in df}
    if not any(out.values()):
        if hit:
            return hit[1]
        raise Unavailable("no rows")
    with _lock:
        _cache[key] = (time.time(), out)
    return out
