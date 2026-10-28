"""
Market data for the V3 research (cached in v3/cache so every experiment uses identical data).

  5-minute (≈60 days, Yahoo's limit): Nifty, Sensex, NIFTYBEES volume, Brent
  60-minute (≈2 years):               Nifty, Sensex, NIFTYBEES volume, Brent  — robustness lab
  daily:                              India VIX

Indices carry no volume on Yahoo, so NIFTYBEES (the Nifty ETF) volume is the volume proxy
for both indices (it reflects overall cash-market participation).
"""
import os, pickle, warnings

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
IST = "Asia/Kolkata"
YAHOO = {"NIFTY": "^NSEI", "SENSEX": "^BSESN"}


def _dl(sym, period, interval):
    import yfinance as yf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(sym, period=period, interval=interval, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])


def _session(df):
    df = df.copy()
    df.index = df.index.tz_convert(IST)
    return df.between_time("09:15", "15:25")


def load(tf="5m", refresh=False):
    """Returns dict(idx={NIFTY: df, SENSEX: df}, vol=Series, vix={date: iv}, brent=Series(UTC), tf=minutes)."""
    path = os.path.join(CACHE, f"data_{tf}.pkl")
    if os.path.exists(path) and not refresh:
        with open(path, "rb") as f:
            return pickle.load(f)
    period = "60d" if tf == "5m" else "730d"
    interval = "5m" if tf == "5m" else "60m"
    idx = {}
    for n, sym in YAHOO.items():
        d = _session(_dl(sym, period, interval))
        idx[n] = pd.DataFrame(dict(o=d["Open"], h=d["High"], l=d["Low"], c=d["Close"]))
    etf = _session(_dl("NIFTYBEES.NS", period, interval))["Volume"].astype(float)
    vix = _dl("^INDIAVIX", "5y" if tf != "5m" else "1y", "1d")["Close"]
    try:
        brent = _dl("BZ=F", period, interval)["Close"]
        brent.index = brent.index.tz_convert("UTC")
    except Exception:
        brent = pd.Series(dtype=float)
    out = dict(idx=idx, vol=etf, vix={d.date(): float(v) / 100 for d, v in vix.items()},
               brent=brent, tf=5 if tf == "5m" else 60)
    with open(path, "wb") as f:
        pickle.dump(out, f)
    return out


if __name__ == "__main__":
    for tf in ("5m", "60m"):
        d = load(tf, refresh=True)
        print(tf, {n: (len(x), str(x.index[0].date()), str(x.index[-1].date())) for n, x in d["idx"].items()},
              "vol", len(d["vol"]), "brent", len(d["brent"]), "vix days", len(d["vix"]))
