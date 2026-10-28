"""
Free paper-trading feed — no Zerodha keys or login needed.

Stands in for the few KiteConnect calls the bot uses in paper mode:
  ltp()             index prices from Yahoo Finance (^NSEI, ^BSESN), may lag ~1 min
                    option prices MODELLED with Black-Scholes (IV = India VIX)
  instruments()     a synthetic weekly option chain (Nifty Tue, Sensex Thu expiry)
  historical_data() recent 5-min index candles from Yahoo for warm-up

Good for testing the strategy and the app. Real fills/premiums on Zerodha will differ —
switch Paper feed to "zerodha" once you have API keys for real option prices.
"""
import math, time
from datetime import datetime, date, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from agents import market  # Yahoo access with 429 handling, shared with the agents

IST = ZoneInfo("Asia/Kolkata")
YAHOO = {"NSE:NIFTY 50": "^NSEI", "BSE:SENSEX": "^BSESN", "NSE:NIFTY BANK": "^NSEBANK"}
TOKENS = {256265: "^NSEI", 265: "^BSESN", 260105: "^NSEBANK"}
CHAINS = {  # exchange -> (name, index key, strike step, lot, expiry weekday)
    "NFO": ("NIFTY", "NSE:NIFTY 50", 50, 65, 1),
    "BFO": ("SENSEX", "BSE:SENSEX", 100, 20, 3),
}
RATE = 0.065
MAX_BAR_AGE = 300    # seconds; the latest Yahoo 1-min bar normally starts 60-120 s ago


class StaleData(Exception):
    pass


def _market_open():
    t = datetime.now(IST)
    return t.weekday() < 5 and dtime(9, 16) <= t.time() <= dtime(15, 30)


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, t_years, iv, kind):
    t = max(t_years, 1 / (365 * 24 * 12))
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t) * _ncdf(d2)
    return strike * math.exp(-RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1)


class FreeFeed:
    def __init__(self, api_key=None):
        self._px = {}        # key -> (price, fetched_at)
        self._vix = (0.13, 0.0)
        self._opts = {}      # "NFO:SYMBOL" -> dict(index_key, strike, kind, expiry)

    def set_access_token(self, _):
        pass

    # ---------------------------------------------------------------- prices
    def _index(self, key):
        """Latest index price. Raises StaleData instead of quietly serving an old price:
        the bot must not enter (or manage exits) on a price that stopped updating."""
        p, ts = self._px.get(key, (None, 0))
        if p is not None and time.time() - ts < 4:
            return p
        d = market.chart(YAHOO[key], "1m", "1d", max_age=10)  # Yahoo lags ~1 min anyway
        age = time.time() - d["t"][-1]
        if d.get("stale") or (_market_open() and age > MAX_BAR_AGE):
            raise StaleData(f"{key}: last Yahoo bar is {age / 60:.0f} min old")
        p = float(d["c"][-1])
        self._px[key] = (p, time.time())
        return p

    def _iv(self):
        v, ts = self._vix
        if time.time() - ts > 600:
            try:
                v = market.last("^INDIAVIX", max_age=600) / 100
            except Exception:
                pass
            self._vix = (v, time.time())
        return v

    def ltp(self, keys):
        out = {}
        for k in keys:
            if k in YAHOO:
                out[k] = {"last_price": round(self._index(k), 2)}
            elif k in self._opts:
                o = self._opts[k]
                spot = self._index(o["index_key"])
                t = (o["expiry_dt"] - datetime.now(IST)).total_seconds() / (365 * 24 * 3600)
                px = bs_price(spot, o["strike"], t, self._iv(), o["kind"])
                out[k] = {"last_price": round(max(0.05, round(px / 0.05) * 0.05), 2)}
            else:
                raise KeyError(f"free feed doesn't know {k}")
        return out

    def quote(self, keys):
        """Like Kite's quote(), but the free feed has no order book, volume or OI — only a price.
        Scalper V2 then scores option liquidity as 'unknown' instead of pretending it passed."""
        return {k: dict(v, depth=None, volume=None, oi=None) for k, v in self.ltp(keys).items()}

    # ---------------------------------------------------------------- option chain
    def instruments(self, exchange):
        name, idx_key, step, lot, wd = CHAINS[exchange]
        spot = self._index(idx_key)
        today = date.today()
        exp = today + timedelta(days=(wd - today.weekday()) % 7)
        if exp == today and datetime.now(IST).time() > dtime(15, 30):
            exp += timedelta(days=7)
        atm = round(spot / step) * step
        rows = []
        for e in (exp, exp + timedelta(days=7)):
            exp_dt = datetime.combine(e, dtime(15, 30), tzinfo=IST)
            for k in range(-20, 21):
                strike = atm + k * step
                for kind in ("CE", "PE"):
                    sym = f"{name}{e:%y%m%d}{int(strike)}{kind}"
                    self._opts[f"{exchange}:{sym}"] = dict(index_key=idx_key, strike=strike, kind=kind, expiry_dt=exp_dt)
                    rows.append(dict(name=name, instrument_type=kind, strike=float(strike), expiry=e,
                                     tradingsymbol=sym, lot_size=lot, exchange=exchange))
        return rows

    # ---------------------------------------------------------------- history
    def historical_data(self, token, start, end, interval):
        mins = int(interval.replace("minute", "") or 1)   # Kite: "minute" = 1-min, "5minute" = 5-min
        d = market.chart(TOKENS[token], f"{mins}m", "5d", max_age=60)
        return [dict(date=datetime.fromtimestamp(ts, timezone.utc).astimezone(IST),
                     open=d["o"][i], high=d["h"][i], low=d["l"][i], close=d["c"][i])
                for i, ts in enumerate(d["t"])]

    # paper mode never calls order functions, but keep them safe
    def place_order(self, **_):
        raise RuntimeError("Free feed can't place orders — paper mode only.")
