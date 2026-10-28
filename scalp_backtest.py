"""
Backtest the scalper (scalper.py rules) on 1-minute index data.

    python scalp_backtest.py                 # settings from config / Settings page
    python scalp_backtest.py --instrument BOTH --lots 1
    python scalp_backtest.py --strategy both      # EMA-cross vs volume-spike breakout side by side

Data    : Yahoo 1-minute candles — only the last ~30 days exist (about 20 trading days).
Options : ITM premiums are MODELLED (Black-Scholes, IV = previous day's India VIX). Premium highs and
          lows inside each minute are taken at the index high/low, and the stop is checked first.
Volume  : indices have no volume, so the volume strategy uses NIFTYBEES 1-minute volume (SCALP_VOL_SYMBOL)
          for both Nifty and Sensex, matched minute by minute.
Costs   : charges + slippage + half the bid/ask spread on every order. With a 5-point stop these
          matter a lot, so the report shows charges-only, base and conservative fills side by side.
"""
import argparse, datetime as dt, math, os, pickle, warnings
from datetime import timedelta

import pandas as pd

import config
import strategy
from backtest import bs_price, next_expiry, years_to
from v3 import costs as C

warnings.filterwarnings("ignore")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v3", "cache", "scalp_1m.pkl")
SCEN = {"charges only": "v2_model", "base fills": "base", "conservative fills": "conservative"}


def load(refresh=False):
    if os.path.exists(CACHE) and not refresh and dt.datetime.fromtimestamp(os.path.getmtime(CACHE)).date() == dt.date.today():
        with open(CACHE, "rb") as f:
                data = pickle.load(f)
        if "vol" in data and "bank" in data:
            return data
    import yfinance as yf
    out = {}
    end = dt.date.today() + timedelta(days=1)
    syms = {n: spec["yahoo"] for n, spec in config.INSTRUMENT_SPECS.items()}
    syms["_vol"] = config.SCALP_VOL_SYMBOL
    syms["_bank"] = "^NSEBANK"            # Scalper V2 cross-index confirmation for Nifty
    for n, sym in syms.items():
        frames = []
        for k in range(5):
            e = end - timedelta(days=7 * k); s = max(e - timedelta(days=7), dt.date.today() - timedelta(days=29))
            if s >= e:
                break
            try:
                d = yf.download(sym, start=s, end=e, interval="1m", progress=False, auto_adjust=False)
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                frames.append(d)
            except Exception:
                pass
        df = pd.concat([f for f in frames if len(f)]).sort_index()
        df = df[~df.index.duplicated()].dropna(subset=["Close"])
        df.index = df.index.tz_convert("Asia/Kolkata")
        out[n] = df.between_time("09:15", "15:29")
    vol = out.pop("_vol")["Volume"].astype(float)
    bank = out.pop("_bank")
    vix = yf.download("^INDIAVIX", period="3mo", interval="1d", progress=False, auto_adjust=False)["Close"]
    vix = vix.iloc[:, 0] if hasattr(vix, "columns") else vix
    vixd = {d.date(): float(v) / 100 for d, v in vix.items()}
    try:
        b = yf.download("BZ=F", period="1mo", interval="5m", progress=False, auto_adjust=False)["Close"]
        b = b.iloc[:, 0] if hasattr(b, "columns") else b
        b.index = b.index.tz_convert("UTC")
    except Exception:
        b = pd.Series(dtype=float)
    data = dict(idx=out, vix=vixd, brent=b, vol=vol, bank=bank)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump(data, f)
    return data


def crude_change(brent, ts):
    if brent.empty:
        return None
    upto = brent[brent.index <= ts.tz_convert("UTC")]
    if upto.empty:
        return None
    day = upto.index[-1].date()
    prior = upto[[x.date() < day for x in upto.index]]
    return None if prior.empty else (upto.iloc[-1] - prior.iloc[-1]) / prior.iloc[-1] * 100


def run(data, names, lots, cost_key, sl=None, tg=None, on=None, strat=None, quiet=True):
    sc = C.SCENARIOS[cost_key]
    sl = sl if sl is not None else config.SCALP_SL_POINTS
    tg = tg if tg is not None else config.SCALP_TARGET_POINTS
    on_prem = (on or config.SCALP_POINTS_ON) == "premium"
    be = config.SCALP_BE_AT_POINTS
    cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    cfg.update(MIN_ATR_PCT=config.SCALP_MIN_ATR_PCT, RR=tg / sl, SCALP_STRATEGY=strat or config.SCALP_STRATEGY)
    vol = data["vol"]
    vdays = sorted(data["vix"])

    def iv(day):
        prev = [d for d in vdays if d < day]
        return data["vix"][prev[-1]] if prev else 0.13

    def at(ts, s):
        h, m = map(int, s.split(":")); return ts.replace(hour=h, minute=m, second=0)

    trades, hist, pos, last_exit, dstate = [], {n: [] for n in names}, {}, {}, {}
    rows = {n: data["idx"][n] for n in names}
    first_day = {n: sorted(set(rows[n].index.date))[0] for n in names}
    timeline = sorted(set().union(*[set(rows[n].index) for n in names]))

    def prem(p, ts, level):
        return bs_price(level, p["strike"], years_to(p["exp"], ts), p["iv"], p["side"])

    def close(n, ts, level_idx, px_mid, reason):
        p = pos.pop(n)
        sell = C.fill(max(0.05, px_mid), "sell", sc)
        gross = (sell - p["buy"]) * p["qty"]
        ch = C.charges(p["buy"] * p["qty"], sell * p["qty"], sc)
        net = gross - ch
        st = dstate[ts.date()]
        st["pnl"] += net; st["n"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -config.SCALP_MAX_DAILY_LOSS or st["consec"] >= config.SCALP_MAX_CONSECUTIVE_LOSSES
                or st["n"] >= config.SCALP_MAX_TRADES_PER_DAY):
            st["locked"] = True
        last_exit[n] = ts
        trades.append(dict(date=ts.date(), index=n, side=p["side"], strike=p["strike"], entry_time=p["t"].strftime("%H:%M"),
                           exit_time=ts.strftime("%H:%M"), hold_min=int((ts - p["t"]).total_seconds() // 60),
                           idx_entry=round(p["idx"], 1), idx_exit=round(level_idx, 1), entry=round(p["buy"], 2),
                           exit=round(sell, 2), gross=round(gross), charges=round(ch), net=round(net), reason=reason))

    for ts in timeline:
        for n in names:
            if ts not in rows[n].index:
                continue
            r = rows[n].loc[ts]
            day = ts.date()
            v = vol.get(ts)
            c = dict(o=float(r["Open"]), h=float(r["High"]), l=float(r["Low"]), c=float(r["Close"]), day=day,
                     v=float(v) if v is not None and v == v else None)
            st = dstate.setdefault(day, dict(pnl=0.0, n=0, consec=0, locked=False))
            close_ts = ts + timedelta(minutes=1)
            if n in pos:
                p = pos[n]
                if ts >= at(ts, config.SQUARE_OFF_AT):
                    close(n, ts, c["o"], prem(p, ts, c["o"]), "square-off")
                else:
                    up = p["side"] == "CE"
                    worst_i, best_i = (c["l"], c["h"]) if up else (c["h"], c["l"])
                    if on_prem:
                        lo_p, hi_p = prem(p, close_ts, worst_i), prem(p, close_ts, best_i)
                        if lo_p <= p["stop"]:
                            close(n, close_ts, worst_i, p["stop"], "stop at cost" if p["be"] else "stop-loss")
                        elif hi_p >= p["target"]:
                            close(n, close_ts, best_i, p["target"], f"target {tg:g}")
                        elif be and not p["be"] and hi_p - p["ref"] >= be:
                            p["stop"], p["be"] = p["ref"] + p["cost_unit"], True
                    else:
                        d = 1 if up else -1
                        if (worst_i - p["stop"]) * d <= 0:
                            close(n, close_ts, p["stop"], prem(p, close_ts, p["stop"]), "stop at cost" if p["be"] else "stop-loss")
                        elif (best_i - p["target"]) * d >= 0:
                            close(n, close_ts, p["target"], prem(p, close_ts, p["target"]), f"target {tg:g}")
                        elif be and not p["be"] and (best_i - p["ref"]) * d >= be:
                            p["stop"], p["be"] = p["ref"] + d * strategy.cost_points(p["buy"], p["qty"]), True
                    if n in pos and config.SCALP_MAX_HOLD_MIN and (close_ts - p["t"]).total_seconds() >= config.SCALP_MAX_HOLD_MIN * 60:
                        close(n, close_ts, c["c"], prem(p, close_ts, c["c"]), "time stop")
            hist[n].append(c); hist[n] = hist[n][-400:]
            if day == first_day[n] or n in pos or st["locked"] or pos:
                continue
            if not (at(ts, config.SCALP_NO_ENTRY_BEFORE) <= close_ts <= at(ts, config.SCALP_NO_ENTRY_AFTER)):
                continue
            if n in last_exit and (close_ts - last_exit[n]).total_seconds() < config.SCALP_COOLDOWN_MIN * 60:
                continue
            sig = strategy.scalp_signal(hist[n], cfg)
            if not sig:
                continue
            ch = crude_change(data["brent"], close_ts)
            if strategy.agent_veto(sig["side"], {"change_pct": ch} if ch is not None else None, None, cfg):
                continue
            spec = config.INSTRUMENT_SPECS[n]
            p = dict(side=sig["side"], strike=strategy.itm_strike(c["c"], sig["side"], spec["step"], config.ITM_STRIKES),
                     exp=next_expiry(close_ts, spec["expiry_weekday"]), iv=iv(day), t=close_ts, idx=c["c"],
                     qty=spec["lot"] * lots, be=False)
            mid = prem(p, close_ts, c["c"])
            if mid * p["qty"] > config.MAX_PREMIUM_PER_TRADE * lots / max(config.SCALP_LOTS, 1):
                continue
            p["buy"] = C.fill(mid, "buy", sc)
            p["cost_unit"] = strategy.cost_per_unit(p["buy"], p["qty"])
            worst = p["qty"] * ((sl if on_prem else sl * 0.7) + p["cost_unit"])
            if worst > config.SCALP_MAX_DAILY_LOSS * lots / max(config.SCALP_LOTS, 1) + st["pnl"]:
                continue
            if on_prem:
                p["ref"], p["stop"], p["target"] = p["buy"], p["buy"] - sl, p["buy"] + tg
            else:
                d = 1 if sig["side"] == "CE" else -1
                p["ref"], p["stop"], p["target"] = c["c"], c["c"] - d * sl, c["c"] + d * tg
            pos[n] = p
    for n in list(pos):
        last = rows[n].index[-1]
        close(n, last, float(rows[n]["Close"].iloc[-1]), prem(pos[n], last, float(rows[n]["Close"].iloc[-1])), "end of data")
    return pd.DataFrame(trades)


def stats(t):
    if t.empty:
        return dict(trades=0, win=0, net=0, avg=0, pf=0, dd=0, worst_day=0, charges=0)
    w, l = t[t.net > 0], t[t.net <= 0]
    eq = t.net.cumsum()
    return dict(trades=len(t), win=round(100 * len(w) / len(t)), net=int(t.net.sum()), avg=int(t.net.mean()),
                pf=round(w.net.sum() / -l.net.sum(), 2) if len(l) and l.net.sum() < 0 else float("inf"),
                dd=int((eq - eq.cummax().clip(lower=0)).min()), worst_day=int(t.groupby("date").net.sum().min()),
                charges=int(t.charges.sum()), avg_hold=round(t.hold_min.mean(), 1))


STRATS = {"ema": "EMA-cross (main bot rules)", "volume": "Volume-spike breakout"}


def report(data, names, lots, strat):
    print(f"\n##### Strategy: {STRATS[strat]} #####")
    rows, main = {}, None
    for lab, key in SCEN.items():
        t = run(data, names, lots, key, strat=strat)
        rows[lab] = stats(t)
        if key == "base":
            main = t
    print(pd.DataFrame(rows).T.to_string())
    if main is not None and len(main):
        print("\nExit reasons (base fills):\n" + main.reason.value_counts().to_string())
        print("\nBy index / side (base fills):\n" + main.groupby(["index", "side"]).net.agg(["count", "sum"]).to_string())
        print("\nDay-wise net (base fills):")
        for d, v in main.groupby("date").net.sum().items():
            print(f"  {d}  ₹{v:>8,.0f}")
    print("\nOther stop/target sizes (base fills) — for context, not optimised:")
    alt = {}
    for s_, t_, o_ in ((5, 10, "premium"), (5, 10, "index"), (10, 20, "premium"), (10, 20, "index"), (15, 30, "index")):
        alt[f"{s_}/{t_} {o_} pts"] = stats(run(data, names, lots, "base", s_, t_, o_, strat=strat))
    print(pd.DataFrame(alt).T.to_string())
    if main is not None:
        main.insert(0, "strategy", strat)
    return main


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=config.SCALP_LOTS)
    ap.add_argument("--instrument", default=config.TRADE_INSTRUMENTS, choices=["NIFTY", "SENSEX", "BOTH"])
    ap.add_argument("--strategy", default=config.SCALP_STRATEGY, choices=["ema", "volume", "both"])
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    names = ["NIFTY", "SENSEX"] if a.instrument == "BOTH" else [a.instrument]
    data = load(a.refresh)
    days = sorted(set().union(*[set(data["idx"][n].index.date) for n in names]))
    on = config.SCALP_POINTS_ON
    print(f"=== Scalper backtest — {', '.join(names)}, {len(days) - 1} days ({days[1]} to {days[-1]}), {a.lots} lot(s), "
          f"ITM {config.ITM_STRIKES}, SL {config.SCALP_SL_POINTS:g} / target {config.SCALP_TARGET_POINTS:g} {on} pts ===")
    if a.strategy in ("volume", "both"):
        print(f"Volume rule: {config.SCALP_VOL_SYMBOL} volume >= {config.SCALP_VOL_MULT:g}x its {config.SCALP_VOL_AVG_N}-candle "
              f"average, close breaks the last {config.SCALP_VOL_BREAKOUT_N} candles, on the VWAP/EMA{config.EMA_SLOW} side.")
    strats = ["ema", "volume"] if a.strategy == "both" else [a.strategy]
    trades = [t for t in (report(data, names, a.lots, s_) for s_ in strats) if t is not None and len(t)]
    if trades:
        pd.concat(trades).to_csv("scalp_backtest_trades.csv", index=False)
    print("\nReminder: 1-minute Yahoo data, modelled option prices — real 5-point scalps depend heavily on fills.")
