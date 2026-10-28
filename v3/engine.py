"""
V3 research backtest engine. With every V3 switch off and COST="v2_model" it reproduces
backtest.py (Strategy V2), so V2 and V3 are measured by the same code.

Bar timing (no look-ahead): features and signals use bars up to and including bar i; the order
is placed at bar i's close (or the next bar's open with execution delay). Exits are checked from
the following bar, stop first (conservative when stop and target sit in the same bar).
Option premiums are MODELLED (Black-Scholes, IV = India VIX) — not real fills.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd

import strategy
from backtest import bs_price, next_expiry, years_to
from . import costs as C
from . import features, market_quality, regime, score as S

SPECS = {"NIFTY": dict(lot=65, step=50, expiry_wd=1), "SENSEX": dict(lot=20, step=100, expiry_wd=3)}
ITM_DELTA = 0.7

DEFAULTS = dict(
    INDICES=("NIFTY", "SENSEX"), LOTS=4, ITM_STRIKES=1, MAX_OPEN_POSITIONS=1,
    NO_ENTRY_BEFORE="09:30", NO_ENTRY_AFTER="13:30", SQUARE_OFF_AT="15:15",
    COST="v2_model", IV_SAME_DAY=False, EVAL_FROM=None, EVAL_TO=None,
    # ---- signal engine (V2 rules)
    SIGNAL_MODE="v2",                 # "v2" = strategy.signal_v2; "relaxed" = trend-side EMA cross, score decides
    HTF_FILTER=True, ADX_MIN=0, EMA_FAST=9, EMA_SLOW=21, EMA_TREND=50, CROSS_LOOKBACK=3, RSI_PERIOD=14,
    RSI_BUY_CE=55, RSI_BUY_PE=45, RSI_MAX=75, MIN_ATR_PCT=0.05, RR=2.0,
    USE_CRUDE_VETO=True, CRUDE_BLOCK_PCT=1.0,
    # ---- exits
    BE_AT_R=1.0, TRAIL_START_R=0, TRAIL_R=1.0, PARTIAL_AT_R=0, PARTIAL_FRAC=0.5, TIME_STOP_MIN=0,
    # ---- V3 switches (all off = V2)
    ENABLE_SCORE_FILTER=False, MIN_SCORE=60, WEIGHTS=None,
    ENABLE_TREND_FILTER=False,
    ENABLE_SIDEWAYS_FILTER=False, SIDEWAYS_MODE="raise", SIDEWAYS_MIN_SCORE=75, SIDEWAYS_VOTES=3,
    ENABLE_REGIME_FILTER=False, REGIME_ACTIONS=None, RESTRICT_SCORE_ADD=10,
    ENABLE_TIME_FILTER=False, TIME_BLOCKS=(),          # [(from_min, to_min)] on bar-close time
    ENABLE_DYNAMIC_SL=False, SL_MODE="v2", SL_ATR_MULT=1.0, SL_PCT=0.25, SL_MIN_ATR=0.5, SL_MAX_ATR=2.0,
    ENABLE_DYNAMIC_TARGET=False,                       # enables partial exits / trailing
    ENABLE_DYNAMIC_SIZING=False, SIZE_HIGH_SCORE=75, LOTS_MED=2, LOSS_STREAK_SIZING=None,
    ENABLE_MARKET_QUALITY=False, MQ_THRESHOLDS=None,
    ENABLE_NEWS_FILTER=False, NEWS=None, NEWS_MEDIUM_SCORE_ADD=10,
    ENABLE_SENSEX_FILTER=False, SENSEX_MIN_SCORE=70, SENSEX_ENABLED=True,
    # ---- risk
    MAX_DAILY_LOSS=12000, MAX_CONSECUTIVE_LOSSES=2, MAX_TRADES_PER_DAY=4, MAX_PREMIUM_PER_LOT=60000,
    ENABLE_RISK_BUDGET=False, MAX_OPEN_RISK=6000, MAX_DRAWDOWN=None,
)


def _mins(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


def _crude_pct(close_ts, brent):
    """Brent % change at each time vs the previous UTC session's last price (the live agent's rule)."""
    if brent is None or brent.empty:
        return np.full(len(close_ts), np.nan)
    b = brent.sort_index()
    bd = np.array([t.date() for t in b.index])
    last_by_day = pd.Series(b.values, index=bd).groupby(level=0).last()
    prev_map = last_by_day.shift(1).to_dict()
    t_utc = close_ts.tz_convert("UTC")
    pos = np.searchsorted(b.index.values, t_utc.values, side="right") - 1
    out = np.full(len(close_ts), np.nan)
    vals = b.values
    for k, p in enumerate(pos):
        if p >= 0:
            pv = prev_map.get(bd[p])
            if pv and not math.isnan(pv):
                out[k] = (vals[p] - pv) / pv * 100
    return out


def prepare(data):
    tf = data["tf"]
    P = dict(tf=tf, feats={}, rows={}, sig={}, ts={})
    vix = data["vix"]
    vdays = sorted(vix)

    def vix_before(d, same):
        k = np.searchsorted(vdays, d, side="right" if same else "left") - 1
        return vix[vdays[k]] if k >= 0 else 0.13

    for n, df in data["idx"].items():
        f = features.build(df, data["vol"], tf=tf, htf_k=3, expiry_weekday=SPECS[n]["expiry_wd"])
        f["regime"] = regime.classify(f)
        f["side_votes"] = regime.sideways_votes(f)
        f["crude"] = _crude_pct(f.index + pd.Timedelta(minutes=tf), data["brent"])
        days = sorted(set(f.date))
        iv_prev = {d: vix_before(d, False) for d in days}
        iv_same = {d: vix_before(d, True) for d in days}
        prev2 = {}
        for d in days:
            k = np.searchsorted(vdays, d, side="left") - 1
            prev2[d] = (vix[vdays[k]] / vix[vdays[k - 1]] - 1) * 100 if k >= 1 else np.nan
        f["iv_prev"] = f.date.map(iv_prev)
        f["iv_same"] = f.date.map(iv_same)
        f["vix_chg"] = f.date.map(prev2)
        P["feats"][n] = f
        P["rows"][n] = f.to_dict("records")
        P["ts"][n] = list(f.index)
    allts = sorted(set().union(*[set(x) for x in P["ts"].values()]))
    pos = {n: {t: i for i, t in enumerate(P["ts"][n])} for n in P["ts"]}
    P["events"] = [(t, [(n, pos[n][t]) for n in ("NIFTY", "SENSEX") if t in pos.get(n, {})]) for t in allts]
    P["days"] = sorted(set(t.date() for t in allts))
    return P


def _v2_signals(P, n, cfg):
    keys = ("HTF_FILTER", "ADX_MIN", "EMA_FAST", "EMA_SLOW", "EMA_TREND", "CROSS_LOOKBACK", "RSI_PERIOD",
            "RSI_BUY_CE", "RSI_BUY_PE", "RSI_MAX", "MIN_ATR_PCT", "RR")
    k = (n,) + tuple(cfg[x] for x in keys)
    if k not in P["sig"]:
        sc = {x: cfg[x] for x in keys}
        f = P["feats"][n]
        bars = [dict(o=o, h=h, l=l, c=c) for o, h, l, c in zip(f.o, f.h, f.l, f.c)]
        P["sig"][k] = [strategy.signal_v2(bars[max(0, i - 399):i + 1], sc) for i in range(len(bars))]
    return P["sig"][k]


def _relaxed_signal(rows, i, cfg):
    r = rows[i]
    if i < 60 or r["atr_pct"] < cfg["MIN_ATR_PCT"]:
        return None
    look = cfg["CROSS_LOOKBACK"]
    diffs = [rows[j]["ema9"] - rows[j]["ema21"] for j in range(i - look, i + 1)]
    lo3 = min(rows[j]["l"] for j in range(i - 2, i + 1))
    hi3 = max(rows[j]["h"] for j in range(i - 2, i + 1))
    a, c = r["atr"], r["c"]
    if diffs[-1] > 0 and any(x <= 0 for x in diffs[:-1]) and c > r["ema50"]:
        risk = min(max(c - lo3, 0.6 * a), 1.5 * a)
        return dict(side="CE", entry=c, risk=risk, why="trend-side EMA cross up")
    if diffs[-1] < 0 and any(x >= 0 for x in diffs[:-1]) and c < r["ema50"]:
        risk = min(max(hi3 - c, 0.6 * a), 1.5 * a)
        return dict(side="PE", entry=c, risk=risk, why="trend-side EMA cross down")
    return None


def _stop_distance(rows, i, d, sig_risk, cfg):
    """Stop distance in index points, or None to skip the trade.

    v2       recent 3-bar swing clamped to 0.6–1.5 ATR (from the signal)
    atr      SL_ATR_MULT × ATR
    pct      SL_PCT % of the index
    recent   recent 3-bar swing (+0.1 ATR buffer), clamped to [SL_MIN_ATR, SL_MAX_ATR] ATR
    structure last CONFIRMED swing point (+0.1 ATR); skipped when it is beyond SL_MAX_ATR
    hybrid   max(recent swing, SL_ATR_MULT × ATR), clamped to [SL_MIN_ATR, SL_MAX_ATR] ATR
    """
    if not cfg["ENABLE_DYNAMIC_SL"] or cfg["SL_MODE"] == "v2":
        return sig_risk
    r = rows[i]; a, c = r["atr"], r["c"]
    mode, k = cfg["SL_MODE"], cfg["SL_ATR_MULT"]
    lo, hi = cfg["SL_MIN_ATR"] * a, cfg["SL_MAX_ATR"] * a
    clamp = lambda x: min(max(x, lo), hi)
    ext = min(rows[j]["l"] for j in range(i - 2, i + 1)) if d > 0 else max(rows[j]["h"] for j in range(i - 2, i + 1))
    recent = (c - ext) * d + 0.1 * a
    if mode == "atr":
        return k * a
    if mode == "pct":
        return c * cfg["SL_PCT"] / 100
    if mode == "recent":
        return clamp(recent)
    if mode == "structure":
        sw = r["swing_low"] if d > 0 else r["swing_high"]
        if sw != sw or (c - sw) * d <= 0:
            return clamp(recent)
        dist = (c - sw) * d + 0.1 * a
        return None if dist > hi else max(dist, lo)
    if mode == "hybrid":
        return clamp(max(recent, k * a))
    raise ValueError(mode)


def run(P, cfg_over=None):
    cfg = {**DEFAULTS, **(cfg_over or {})}
    sc = C.SCENARIOS[cfg["COST"]]
    tf = P["tf"]
    names = [n for n in cfg["INDICES"] if n in P["feats"] and (n != "SENSEX" or cfg["SENSEX_ENABLED"])]
    sigs = {n: _v2_signals(P, n, cfg) for n in names} if cfg["SIGNAL_MODE"] == "v2" else {}
    weights = cfg["WEIGHTS"] or S.WEIGHTS
    ra = cfg["REGIME_ACTIONS"] or {}
    news = cfg["NEWS"] or {}
    sq_m, nb_m, na_m = _mins(cfg["SQUARE_OFF_AT"]), _mins(cfg["NO_ENTRY_BEFORE"]), _mins(cfg["NO_ENTRY_AFTER"])
    ev_from, ev_to = cfg["EVAL_FROM"], cfg["EVAL_TO"]
    streak_sz = cfg["LOSS_STREAK_SIZING"] or {}

    pos, pend, trades, rejects, dstate = {}, {}, [], [], {}
    eq = {"v": 0.0, "peak": 0.0, "halt": False}

    def premium(n, p, ts, level):
        return bs_price(level, p["strike"], years_to(p["exp"], ts), p["iv"], p["side"])

    def finish(n, ts, reason):
        p = pos.pop(n)
        net = p["gross"] - p["charges"]
        st = dstate[p["day"]]
        st["pnl"] += net; st["trades"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -cfg["MAX_DAILY_LOSS"] or st["consec"] >= cfg["MAX_CONSECUTIVE_LOSSES"]
                or st["trades"] >= cfg["MAX_TRADES_PER_DAY"]):
            st["locked"] = True
        eq["v"] += net; eq["peak"] = max(eq["peak"], eq["v"])
        if cfg["MAX_DRAWDOWN"] and eq["peak"] - eq["v"] >= cfg["MAX_DRAWDOWN"]:
            eq["halt"] = True
        hold = (ts - p["t"]).total_seconds() / 60
        trades.append(dict(
            date=p["day"], index=n, side=p["side"], entry_time=p["t"].strftime("%H:%M"), exit_time=ts.strftime("%H:%M"),
            hold_min=round(hold), idx_entry=round(p["entry"], 1), idx_exit=round(p["last_exit_level"], 1),
            sl=round(p["sl0"], 1), target=round(p["target"], 1), risk_pts=round(p["risk"], 1),
            strike=p["strike"], itm_pts=round((p["entry"] - p["strike"]) * p["d"], 1), lots=p["lots0"],
            premium=round(p["prem"], 2), gross=round(p["gross"]), charges=round(p["charges"]), net=round(net),
            risk_rs=round(p["risk_rs"]), R=round(net / p["risk_rs"], 2) if p["risk_rs"] > 0 else 0.0,
            reason=reason, partial=p["partial_done"], mfe_R=round(p["mfe"] / p["risk"], 2),
            mae_R=round(p["mae"] / p["risk"], 2), **p["diag"]))

    def exit_lots(n, lots_x, level, ts):
        p = pos[n]
        q = SPECS[n]["lot"] * lots_x
        sell = C.fill(max(0.05, premium(n, p, ts, level)), "sell", sc)
        p["gross"] += (sell - p["prem"]) * q
        p["charges"] += C.charges(0, sell * q, sc, orders=1)
        p["lots"] -= lots_x
        p["last_exit_level"] = level

    def manage(n, r, ts):
        p = pos[n]; d = p["d"]
        close_ts = ts + timedelta(minutes=tf)
        h, l = r["h"], r["l"]
        p["mfe"] = max(p["mfe"], (h - p["entry"]) * d if d > 0 else (p["entry"] - l))
        p["mae"] = max(p["mae"], (p["entry"] - l) if d > 0 else (h - p["entry"]))
        if r["tod"] - tf >= sq_m:
            exit_lots(n, p["lots"], r["o"], ts); finish(n, ts, "square-off time"); return
        if (d > 0 and l <= p["sl"]) or (d < 0 and h >= p["sl"]):
            why = "trailing stop" if p["trailed"] else "stop at cost" if p["be_done"] else "stop-loss"
            exit_lots(n, p["lots"], p["sl"], close_ts); finish(n, close_ts, why); return
        fav = h if d > 0 else l
        dyn = cfg["ENABLE_DYNAMIC_TARGET"]
        if dyn and cfg["PARTIAL_AT_R"] and not p["partial_done"] and p["lots"] >= 2:
            lvl1 = p["entry"] + d * cfg["PARTIAL_AT_R"] * p["risk"]
            if (fav - lvl1) * d >= 0:
                exit_lots(n, max(1, int(p["lots"] * cfg["PARTIAL_FRAC"])), lvl1, close_ts)
                p["partial_done"] = True
                cost = p["entry"] + d * p["cost_pts"]
                if (cost - p["sl"]) * d > 0:
                    p["sl"] = cost
                p["be_done"] = True
        if (fav - p["target"]) * d >= 0:
            exit_lots(n, p["lots"], p["target"], close_ts)
            finish(n, close_ts, f"target 1:{p['rr']:g}" + (" (after partial)" if p["partial_done"] else "")); return
        p["best"] = max(p["best"], h) if d > 0 else min(p["best"], l)
        gained = (p["best"] - p["entry"]) * d / p["risk"]
        if cfg["BE_AT_R"] and not p["be_done"] and gained >= cfg["BE_AT_R"]:
            cost = p["entry"] + d * p["cost_pts"]
            if (cost - p["sl"]) * d > 0:
                p["sl"] = cost
            p["be_done"] = True
        if dyn and cfg["TRAIL_START_R"] and gained >= cfg["TRAIL_START_R"]:
            trail = p["best"] - d * cfg["TRAIL_R"] * p["risk"]
            if (trail - p["sl"]) * d > 0:
                p["sl"], p["trailed"] = trail, True
        if cfg["TIME_STOP_MIN"] and gained < 0.5 and (close_ts - p["t"]).total_seconds() >= cfg["TIME_STOP_MIN"] * 60:
            exit_lots(n, p["lots"], r["c"], close_ts); finish(n, close_ts, "time stop")

    def open_pos(n, rows, i, ts_fill, level, plan):
        spec = SPECS[n]
        d = plan["d"]
        shift = level - plan["entry"]
        iv = rows[i]["iv_same"] if cfg["IV_SAME_DAY"] else rows[i]["iv_prev"]
        p = dict(side=plan["side"], d=d, entry=level, risk=plan["risk"], rr=plan["rr"],
                 sl=plan["sl"] + shift, sl0=plan["sl"] + shift, target=plan["target"] + shift,
                 strike=strategy.itm_strike(plan["entry"], plan["side"], spec["step"], cfg["ITM_STRIKES"]),
                 exp=next_expiry(ts_fill, spec["expiry_wd"]), iv=iv, t=ts_fill, day=ts_fill.date(),
                 be_done=False, trailed=False, partial_done=False, best=level, mfe=0.0, mae=0.0,
                 gross=0.0, charges=0.0, last_exit_level=level, diag=plan["diag"])
        mid = bs_price(level, p["strike"], years_to(p["exp"], ts_fill), iv, p["side"])
        lots = plan["lots"]
        if mid * spec["lot"] > cfg["MAX_PREMIUM_PER_LOT"]:
            return "premium cap"
        p["prem"] = C.fill(mid, "buy", sc)
        q1 = spec["lot"]
        stop_mid = bs_price(p["sl"], p["strike"], years_to(p["exp"], ts_fill), iv, p["side"])
        per_lot = ((p["prem"] - C.fill(max(0.05, stop_mid), "sell", sc)) * q1
                   + C.charges(p["prem"] * q1, stop_mid * q1, sc, orders=2))
        if cfg["ENABLE_RISK_BUDGET"]:
            st = dstate[p["day"]]
            room = min(cfg["MAX_OPEN_RISK"], cfg["MAX_DAILY_LOSS"] + st["pnl"])
            lots = min(lots, int(room // per_lot)) if per_lot > 0 else lots
            if lots < 1:
                return "risk budget"
        p["lots"] = p["lots0"] = lots
        p["risk_rs"] = per_lot * lots
        if cfg["COST"] == "v2_model":
            p["cost_pts"] = strategy.cost_points(p["prem"], q1 * lots)
        else:
            unit = (C.charges(p["prem"] * q1 * lots, p["prem"] * q1 * lots, sc) / (q1 * lots)
                    + 2 * p["prem"] * (sc["slippage"] + sc["half_spread"]))
            p["cost_pts"] = unit / ITM_DELTA
        p["charges"] = C.charges(p["prem"] * q1 * lots, 0, sc, orders=1)
        pos[n] = p
        return None

    for ts, items in P["events"]:
        for n, i in items:
            if n not in names:
                continue
            rows = P["rows"][n]
            r = rows[i]
            day = r["date"]
            st = dstate.setdefault(day, dict(pnl=0.0, trades=0, consec=0, locked=False))
            close_ts = ts + timedelta(minutes=tf)
            # delayed fills execute at this bar's open
            if n in pend:
                plan = pend.pop(n)
                if day == plan["day"]:
                    why = open_pos(n, rows, i, ts, r["o"], plan)
                    if why:
                        rejects.append(dict(plan["rej"], reason=why))
            if n in pos and pos[n]["t"] < close_ts:
                manage(n, r, ts)
            # ---- entries
            if ev_from and day < ev_from or ev_to and day > ev_to:
                continue
            if n in pos or n in pend or not (nb_m <= r["tod"] <= na_m):
                continue
            if cfg["SIGNAL_MODE"] == "v2":
                sig = sigs[n][i]
                if sig:
                    sig = dict(sig)
            else:
                sig = _relaxed_signal(rows, i, cfg)
            if not sig:
                continue
            d = 1 if sig["side"] == "CE" else -1
            dist = _stop_distance(rows, i, d, sig["risk"], cfg)
            sc_val, comp = S.score(r, d, weights, dist or sig["risk"])
            nl = news.get(day, "LOW")
            mq = market_quality.assess(r, d, crude_pct=r["crude"], vix=r["iv_prev"], vix_chg=r["vix_chg"],
                                       news_risk=nl if cfg["ENABLE_NEWS_FILTER"] else "LOW",
                                       crude_block_pct=cfg["CRUDE_BLOCK_PCT"], thresholds=cfg["MQ_THRESHOLDS"])
            diag = dict(score=round(sc_val, 1), regime=r["regime"], side_votes=int(r["side_votes"]),
                        mq=mq["market_quality"], mq_decision=mq["decision"], news=nl,
                        crude=None if r["crude"] != r["crude"] else round(r["crude"], 2),
                        adx=round(r["adx"], 1), rsi=round(r["rsi"], 1), atr=round(r["atr"], 1),
                        atr_rank=None if r["atr_rank"] != r["atr_rank"] else round(r["atr_rank"], 2),
                        vol_ratio=None if r["vol_ratio"] != r["vol_ratio"] else round(r["vol_ratio"], 2),
                        vwap_dist=round(r["vwap_dist"], 2), ext21=round(r["ext21"], 2), htf_dir=int(r["htf_dir"]),
                        expiry_day=bool(r["expiry_day"]), signal_tod=r["tod"],
                        **{f"c_{k}": round(v, 2) for k, v in comp.items()})
            rej = dict(ts=close_ts, index=n, side=sig["side"], **diag)

            def reject(why):
                rejects.append(dict(rej, reason=why))

            # gates, cheapest/most fundamental first
            if eq["halt"]:
                reject("max drawdown halt"); continue
            if st["locked"]:
                reject("daily lock"); continue
            if len(pos) + len(pend) >= cfg["MAX_OPEN_POSITIONS"]:
                reject("max open positions"); continue
            if dist is None:
                reject("stop too wide"); continue
            # required score: 0 unless the score filter (or a stricter-score rule) applies
            need = cfg["MIN_SCORE"] if cfg["ENABLE_SCORE_FILTER"] else 0
            size = 1.0
            if cfg["ENABLE_TIME_FILTER"] and any(a <= r["tod"] < b for a, b in cfg["TIME_BLOCKS"]):
                reject("time filter"); continue
            if cfg["ENABLE_REGIME_FILTER"]:
                act = ra.get(r["regime"], "ALLOW")
                if act == "BLOCK":
                    reject(f"regime {r['regime']}"); continue
                if act == "RESTRICT":
                    need = max(need, cfg["MIN_SCORE"]) + cfg["RESTRICT_SCORE_ADD"]
            if cfg["ENABLE_TREND_FILTER"]:
                short = (r["ema9"] - r["ema21"]) * d > 0 and (r["c"] - r["ema50"]) * d > 0
                if not (short and r["htf_dir"] * d > 0):
                    reject("trend conflict"); continue
            if cfg["ENABLE_SIDEWAYS_FILTER"] and r["side_votes"] >= cfg["SIDEWAYS_VOTES"]:
                if cfg["SIDEWAYS_MODE"] == "reject":
                    reject("sideways market"); continue
                need = max(need, cfg["SIDEWAYS_MIN_SCORE"])
            if cfg["ENABLE_NEWS_FILTER"]:
                if nl == "HIGH":
                    reject("news risk HIGH"); continue
                if nl == "MEDIUM":
                    need = max(need, cfg["MIN_SCORE"]) + cfg["NEWS_MEDIUM_SCORE_ADD"]; size *= 0.5
            if cfg["ENABLE_SENSEX_FILTER"] and n == "SENSEX" and sc_val < cfg["SENSEX_MIN_SCORE"]:
                reject("sensex score"); continue
            if sc_val < need:
                reject(f"score {sc_val:.0f} < {need}"); continue
            if cfg["ENABLE_MARKET_QUALITY"]:
                if mq["decision"] in ("BLOCK", "WAIT"):
                    reject(f"market quality {mq['decision']}"); continue
                if mq["decision"] == "REDUCE_SIZE":
                    size *= 0.5
            elif cfg["USE_CRUDE_VETO"] and r["crude"] == r["crude"]:
                if (d > 0 and r["crude"] >= cfg["CRUDE_BLOCK_PCT"]) or (d < 0 and r["crude"] <= -cfg["CRUDE_BLOCK_PCT"]):
                    reject("crude veto"); continue
            lots = cfg["LOTS"]
            if cfg["ENABLE_DYNAMIC_SIZING"]:
                lots = cfg["LOTS"] if sc_val >= cfg["SIZE_HIGH_SCORE"] else cfg["LOTS_MED"]
                for k in sorted(streak_sz):
                    if st["consec"] >= k:
                        size *= streak_sz[k]
            lots = int(lots * size) if size < 1 else lots
            if size < 1 and lots < 1 and size > 0:
                lots = 1
            if lots < 1:
                reject("size zero"); continue
            rr = cfg["RR"]
            plan = dict(side=sig["side"], d=d, entry=r["c"], risk=dist, rr=rr, sl=r["c"] - d * dist,
                        target=r["c"] + d * rr * dist, lots=lots, day=day, rej=rej,
                        diag=dict(diag, entry_reason=sig.get("why", "")))
            if sc["delay_bars"]:
                pend[n] = plan
            else:
                why = open_pos(n, rows, i, close_ts, r["c"], plan)
                if why:
                    reject(why)
    for n in list(pos):
        rows = P["rows"][n]
        last = P["ts"][n][-1]
        exit_lots(n, pos[n]["lots"], rows[-1]["c"], last)
        finish(n, last, "end of data")
    tr = pd.DataFrame(trades)
    if ev_from or ev_to:
        days = [d for d in P["days"] if (not ev_from or d >= ev_from) and (not ev_to or d <= ev_to)]
    else:
        days = P["days"]
    return dict(trades=tr, rejects=pd.DataFrame(rejects), days=days, cfg=cfg)
