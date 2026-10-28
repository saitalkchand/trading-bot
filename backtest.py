"""
Backtest strategy v2 (same rules as the bot, from strategy.py) on real 5-min index data.

    python backtest.py                                  # settings from config / Settings page
    python backtest.py --days 30 --lots 2 --instrument BOTH

Data    : Nifty (^NSEI) / Sensex (^BSESN) 5-min candles, Brent (BZ=F) 5-min, India VIX — Yahoo, ≤ ~55 days.
Options : ITM premiums are MODELLED with Black-Scholes (IV = India VIX) and weekly expiry
          (Nifty Tue, Sensex Thu) — free historical option prices don't exist. Treat as estimates.
Filters : the volume filter (NIFTYBEES 5-min volume) and VIX-scaled stops are tested, with a table
          comparing each switched off. VIX = the PREVIOUS day's close (no look-ahead).
Agents  : the crude-oil filter IS tested (prints ON vs OFF). The news agent can't be backtested
          (no free archive of intraday headlines) — it only works live/paper.
"""
import argparse, math
from datetime import datetime, timedelta, time as dtime, timezone

import pandas as pd
import yfinance as yf

import config
import strategy

RATE = 0.065


# ---------------------------------------------------------------- option model
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, t_years, iv, kind):
    t = max(t_years, 1 / (365 * 24 * 12))
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * ncdf(d1) - strike * math.exp(-RATE * t) * ncdf(d2)
    return strike * math.exp(-RATE * t) * ncdf(-d2) - spot * ncdf(-d1)


def next_expiry(ts, weekday):
    d = ts.date()
    exp = datetime.combine(d + timedelta(days=(weekday - d.weekday()) % 7), dtime(15, 30), tzinfo=ts.tzinfo)
    return exp + timedelta(days=7) if exp <= ts else exp


def years_to(exp, ts):
    return (exp - ts).total_seconds() / (365 * 24 * 3600)


def charges(buy_val, sell_val):
    brokerage = 40
    stt = 0.0015 * sell_val           # options STT on sell premium, 0.15% from 1 Apr 2026
    txn = 0.0003553 * (buy_val + sell_val)  # NSE options transaction charge
    sebi = 10e-7 * (buy_val + sell_val)
    stamp = 0.00003 * buy_val
    gst = 0.18 * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


# ---------------------------------------------------------------- data
def _dl(sym, period, interval):
    df = yf.download(sym, period=period, interval=interval, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])


def load(names, days):
    period = f"{min(days + 10, 59)}d"
    idx = {}
    for n in names:
        df = _dl(config.INSTRUMENT_SPECS[n]["yahoo"], period, "5m")
        df.index = df.index.tz_convert("Asia/Kolkata")
        idx[n] = df.between_time("09:15", "15:25")
    vix = _dl("^INDIAVIX", "3mo", "1d")["Close"]
    vix_by_day = {d.date(): float(v) / 100 for d, v in vix.items()}
    try:
        brent = _dl("BZ=F", period, "5m")["Close"]
        brent.index = brent.index.tz_convert("UTC")
    except Exception:
        brent = pd.Series(dtype=float)
    return idx, vix_by_day, brent


def load_volume(days):
    """Volume proxy (VOLUME_SYMBOL) 5-min bars as (epoch list, volume list); empty if unavailable."""
    try:
        df = _dl(config.VOLUME_SYMBOL, f"{min(days + 10, 59)}d", "5m")
    except Exception:
        return [], []
    return [int(t.timestamp()) for t in df.index], [float(v or 0) for v in df["Volume"]]


def vix_before(day, vix_by_day):
    """India VIX (in points) at the previous close — what the bot could know at the open."""
    prior = [d for d in vix_by_day if d < day]
    return vix_by_day[max(prior)] * 100 if prior else None


def crude_change(brent, ts):
    """% change of Brent at time ts vs the previous UTC session's last price (same rule as the live agent)."""
    if brent.empty:
        return None
    t = ts.tz_convert("UTC")
    upto = brent[brent.index <= t]
    if upto.empty:
        return None
    day = upto.index[-1].date()
    prior = upto[[d.date() < day for d in upto.index]]
    if prior.empty:
        return None
    return (upto.iloc[-1] - prior.iloc[-1]) / prior.iloc[-1] * 100


def iv_for(day, vix_by_day):
    prior = [d for d in vix_by_day if d <= day]
    return vix_by_day[max(prior)] if prior else 0.13


# ---------------------------------------------------------------- simulation
def run(idx, vix, brent, names, days, lots, use_crude, volume=None, **overrides):
    cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    cfg.update(overrides)
    cfg["USE_CRUDE_AGENT"] = use_crude
    vol_t, vol_v = volume or ([], [])
    cfg["USE_NEWS_AGENT"] = False
    specs = {n: config.INSTRUMENT_SPECS[n] for n in names}
    all_days = sorted(set().union(*[set(idx[n].index.date) for n in names]))
    test_days = set(all_days[-days:])
    tf = timedelta(minutes=config.CANDLE_MINUTES)
    scale = lots / max(config.LOTS, 1)
    max_loss = config.MAX_DAILY_LOSS * scale
    max_prem = config.MAX_PREMIUM_PER_TRADE * scale

    def at(ts, s):
        h, m = map(int, s.split(":")); return ts.replace(hour=h, minute=m, second=0)

    hist = {n: [] for n in names}
    pos, trades, day_state, vetoed = {}, [], {}, 0
    timeline = sorted(set().union(*[set(idx[n].index) for n in names]))
    rows = {n: idx[n] for n in names}

    def opt(p, ts, spot):
        return bs_price(spot, p["strike"], years_to(p["exp"], ts), p["iv"], p["side"])

    def exit_trade(n, ts, level, reason):
        p = pos.pop(n)
        px = max(0.05, opt(p, ts, level))
        gross = (px - p["prem"]) * p["qty"]
        ch = charges(p["prem"] * p["qty"], px * p["qty"])
        net = gross - ch
        st = day_state[ts.date()]
        st["pnl"] += net; st["trades"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -max_loss or st["consec"] >= config.MAX_CONSECUTIVE_LOSSES
                or st["trades"] >= config.MAX_TRADES_PER_DAY):
            st["locked"] = True
        trades.append(dict(date=ts.date(), index=n, side=p["side"], strike=p["strike"],
                           entry_time=p["t"].strftime("%H:%M"), entry=round(p["prem"], 2),
                           exit_time=ts.strftime("%H:%M"), exit=round(px, 2),
                           idx_entry=round(p["entry"], 1), idx_exit=round(level, 1),
                           gross=round(gross), charges=round(ch), net=round(net), reason=reason))

    for ts in timeline:
        day = ts.date()
        for n in names:
            if ts not in rows[n].index:
                continue
            r = rows[n].loc[ts]
            c = dict(o=float(r["Open"]), h=float(r["High"]), l=float(r["Low"]), c=float(r["Close"]))
            if day not in test_days:
                hist[n].append(c); continue
            st = day_state.setdefault(day, dict(pnl=0.0, trades=0, consec=0, locked=False))
            close_ts = ts + tf
            if n in pos:
                if ts >= at(ts, config.SQUARE_OFF_AT):
                    exit_trade(n, ts, c["o"], "square-off time")
                else:
                    p = pos[n]
                    lvl, why = strategy.manage(p, c["h"], c["l"], cfg, now=close_ts, close=c["c"])
                    if lvl is not None:
                        exit_trade(n, close_ts, lvl, why)
                    elif p["be_done"] and cfg.get("COST_STOP_ON_PREMIUM"):
                        worst = c["l"] if p["side"] == "CE" else c["h"]
                        if opt(p, close_ts, worst) <= p["prem"] + p["cost_unit"]:
                            exit_trade(n, close_ts, worst, "stop at cost")
            hist[n].append(c)
            hist[n] = hist[n][-400:]
            if (n in pos or st["locked"] or len(pos) >= config.MAX_OPEN_POSITIONS
                    or not (at(ts, config.NO_ENTRY_BEFORE) <= close_ts <= at(ts, config.NO_ENTRY_AFTER))):
                continue
            sig = strategy.signal_v2(hist[n], cfg)
            if not sig:
                continue
            if cfg.get("VOLUME_FILTER") and vol_t:
                # 5-min proxy bars: the bar starting at ts is the signal candle (minutes=1 picks just that bar)
                rv, _ = strategy.relative_volume(vol_t, vol_v, int(ts.timestamp()), 1, cfg["VOLUME_AVG_N"])
                if rv is not None and rv < cfg["VOLUME_MIN_RVOL"]:
                    continue
            if cfg.get("VIX_ADJUST_EXITS"):
                sig = strategy.vix_scale(sig, vix_before(day, vix), cfg)
            ch = crude_change(brent, close_ts) if use_crude else None
            if strategy.agent_veto(sig["side"], {"change_pct": ch} if ch is not None else None, None, cfg):
                vetoed += 1; continue
            spec = specs[n]
            p = dict(side=sig["side"], entry=sig["entry"], sl=sig["sl"], target=sig["target"], risk=sig["risk"],
                     be_done=False, strike=strategy.itm_strike(sig["entry"], sig["side"], spec["step"], config.ITM_STRIKES),
                     exp=next_expiry(close_ts, spec["expiry_weekday"]), iv=iv_for(day, vix), t=close_ts,
                     qty=spec["lot"] * lots)
            p["prem"] = opt(p, close_ts, sig["entry"])
            if p["prem"] * p["qty"] > max_prem:
                continue
            p["cost_pts"] = strategy.cost_points(p["prem"], p["qty"])
            p["cost_unit"] = strategy.cost_per_unit(p["prem"], p["qty"])
            pos[n] = p
    for n in list(pos):
        last = rows[n].index[-1]
        exit_trade(n, last, float(rows[n]["Close"].iloc[-1]), "end of data")
    return pd.DataFrame(trades), sorted(test_days), vetoed


# ---------------------------------------------------------------- report
def summary(tr):
    if tr.empty:
        return dict(trades=0, win=0, net=0, gross=0, charges=0, avg_win=0, avg_loss=0, dd=0, rr=0)
    w, l = tr[tr.net > 0], tr[tr.net <= 0]
    eq = tr.net.cumsum()
    aw, al = (w.net.mean() if len(w) else 0), (l.net.mean() if len(l) else 0)
    return dict(trades=len(tr), win=len(w) / len(tr) * 100, net=tr.net.sum(), gross=tr.gross.sum(),
                charges=tr.charges.sum(), avg_win=aw, avg_loss=al, dd=(eq - eq.cummax()).min(),
                rr=(aw / abs(al)) if al else 0)


def report(tr, days, names, lots, vetoed, label):
    print(f"\n=== Strategy v2 backtest [{label}] — {', '.join(names)}, {len(days)} days "
          f"({days[0]} to {days[-1]}), {lots} lot(s), ITM {config.ITM_STRIKES}, 1:{config.RR:g} ===")
    if tr.empty:
        print("No trades — filters found no qualifying setups."); return
    s = summary(tr)
    daily = tr.groupby("date")["net"].sum()
    print(f"Trades           : {s['trades']}  (wins {int((tr.net > 0).sum())}, losses {int((tr.net <= 0).sum())}, "
          f"win rate {s['win']:.0f}%)")
    print(f"Gross P&L        : ₹{s['gross']:,.0f}")
    print(f"Charges          : ₹{s['charges']:,.0f}")
    print(f"NET P&L          : ₹{s['net']:,.0f}")
    print(f"Avg win / loss   : ₹{s['avg_win']:,.0f} / ₹{s['avg_loss']:,.0f}   (realised reward:risk 1:{s['rr']:.2f})")
    print(f"Best / worst trade: ₹{tr.net.max():,.0f} / ₹{tr.net.min():,.0f}")
    print(f"Best / worst day : ₹{daily.max():,.0f} ({daily.idxmax()}) / ₹{daily.min():,.0f} ({daily.idxmin()})")
    print(f"Max drawdown     : ₹{s['dd']:,.0f}")
    print(f"Days traded      : {len(daily)}  (green {int((daily > 0).sum())}, red {int((daily <= 0).sum())})")
    if vetoed:
        print(f"Crude agent vetoed {vetoed} signal(s)")
    if len(names) > 1:
        print("\nBy index:\n" + tr.groupby("index")["net"].agg(["count", "sum"]).rename(
            columns={"count": "trades", "sum": "net ₹"}).round(0).to_string())
    print("\nExit reasons:\n" + tr.reason.value_counts().to_string())
    print("\nDay-wise net P&L:")
    for d, v in daily.items():
        print(f"  {d}  ₹{v:>9,.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lots", type=int, default=config.LOTS)
    ap.add_argument("--instrument", default=config.TRADE_INSTRUMENTS, choices=["NIFTY", "SENSEX", "BOTH"])
    ap.add_argument("--no-compare", action="store_true")
    a = ap.parse_args()
    names = ["NIFTY", "SENSEX"] if a.instrument == "BOTH" else [a.instrument]
    idx, vix, brent = load(names, a.days)
    vol = load_volume(a.days)
    if brent.empty:
        print("Note: Brent data unavailable — crude filter can't be tested.")
    if config.VOLUME_FILTER and not vol[0]:
        print(f"Note: {config.VOLUME_SYMBOL} volume unavailable — volume filter not applied.")
    main_crude = bool(config.USE_CRUDE_AGENT) and not brent.empty
    tr, days, vetoed = run(idx, vix, brent, names, a.days, a.lots, main_crude, vol)
    report(tr, days, names, a.lots, vetoed, "crude agent ON" if main_crude else "crude agent OFF")
    tr.to_csv("backtest_trades.csv", index=False)
    if not a.no_compare:
        cols = [("as set", {}), ("volume off", dict(VOLUME_FILTER=False)), ("VIX exits off", dict(VIX_ADJUST_EXITS=False)),
                ("both off", dict(VOLUME_FILTER=False, VIX_ADJUST_EXITS=False))]
        on = dict(VOLUME_FILTER=True, VIX_ADJUST_EXITS=True)
        res = [(lbl, summary(run(idx, vix, brent, names, a.days, a.lots, main_crude, vol, **{**on, **o})[0])) for lbl, o in cols]
        print("\n=== Volume filter & VIX-scaled exits ===")
        print(f"{'':18}" + "".join(f"{lbl:>15}" for lbl, _ in res))
        for k, lbl, f in [("trades", "Trades", "{:.0f}"), ("win", "Win rate %", "{:.0f}"), ("net", "Net P&L ₹", "{:,.0f}"),
                          ("dd", "Max drawdown ₹", "{:,.0f}"), ("rr", "Reward:risk 1:", "{:.2f}")]:
            print(f"{lbl:18}" + "".join(f"{f.format(s[k]):>15}" for _, s in res))
    if not a.no_compare and not brent.empty:
        alt, _, _ = run(idx, vix, brent, names, a.days, a.lots, not main_crude, vol)
        s1, s2 = summary(tr), summary(alt)
        lab1, lab2 = ("ON", "OFF") if main_crude else ("OFF", "ON")
        print(f"\n=== Crude agent {lab1} vs {lab2} ===")
        print(f"{'':18}{'agent ' + lab1:>14}{'agent ' + lab2:>14}")
        for k, lbl, f in [("trades", "Trades", "{:.0f}"), ("win", "Win rate %", "{:.0f}"), ("net", "Net P&L ₹", "{:,.0f}"),
                          ("dd", "Max drawdown ₹", "{:,.0f}"), ("rr", "Reward:risk 1:", "{:.2f}")]:
            print(f"{lbl:18}{f.format(s1[k]):>14}{f.format(s2[k]):>14}")
    print("\nAll trades saved to backtest_trades.csv")
    print("Reminder: option prices are modelled; news agent not included (live/paper only).")
