"""
Scalper V2 backtest — current scalper vs V2 on the SAME data, fills, option model and engine.

    python scalp_v2_backtest.py                  # full report → reports/scalper_v2/
    python scalp_v2_backtest.py --quick          # tests A–D only

Tests
  A  current scalper  : EMA-cross (strategy.signal_v2) on 1-min, 5/10 premium points, stop to cost at +5,
                        its old limits (10 trades, 3 losses, ₹1,500), crude agent on — pinned here so later
                        Settings changes can't move the baseline
  B  V2 strict        : every rule of the V2 spec mandatory
  C  V2 (no agents)   : core filters mandatory, RSI/MACD/structure/volume scored, score >= TRADE_SCORE_MIN
  D  V2 + agents      : C plus the crude-oil agent (the only agent with history; news/sentiment/debate can't
                        be backtested)
  then stop/target and break-even grids, one-at-a-time parameter sensitivity, and a walk-forward.

Honest limits
  * Yahoo keeps ~30 days of 1-min data → ~19 trading days. A walk-forward on that has 2 folds.
  * Option prices are Black-Scholes (IV = previous day's India VIX); no historical bid/ask, option volume
    or OI exist for free, so the liquidity/spread filter is only exercised live — here it scores 7/10.
  * Fills: charges-only, base (0.5% per side) and conservative (0.9% per side + entry one candle late).
"""
import argparse, os, time, warnings
from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

import config
import strategy
import scalp_v2 as V
from backtest import next_expiry
from scalp_backtest import load, crude_change
from v3 import costs as C
from v3.metrics import objective

warnings.filterwarnings("ignore")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "scalper_v2")
FILLS = {"charges only": "v2_model", "base": "base", "conservative": "conservative"}

LEGACY = dict(MODE_="legacy", SCALP_SL_POINTS=5, SCALP_TARGET_POINTS=10, SCALP_BE_AT_POINTS=5, SCALP_MAX_TRADES_PER_DAY=10,
              SCALP_MAX_CONSECUTIVE_LOSSES=3, SCALP_MAX_DAILY_LOSS=1500, SCALP_COOLDOWN_MIN=2, SCALP_NO_ENTRY_BEFORE="09:30",
              SCALP_NO_ENTRY_AFTER="14:30", SCALP_MAX_HOLD_MIN=10, SCALP_MIN_ATR_PCT=0.02, ITM_STRIKES=1, AGENTS=True)

V2 = dict(MODE_="v2", SCALP_MAX_TRADES_PER_DAY=6, SCALP_MAX_CONSECUTIVE_LOSSES=2, SCALP_MAX_DAILY_LOSS=1000,
          SCALP_COOLDOWN_MIN=5, SCALP_NO_ENTRY_BEFORE="09:35", SCALP_NO_ENTRY_AFTER="14:30", SCALP_MAX_HOLD_MIN=10,
          SCALP_DYNAMIC_SL=True, SCALP_MIN_SL_POINTS=5, SCALP_MAX_SL_POINTS=12, SCALP_ATR_SL_MULT=0.8, SCALP_RR=2.0,
          SCALP_SL_POINTS=5, SCALP_BE_AT_R=1.2, SCALP_STRUCTURAL_COOLDOWN=True, SCALP_SIDE_ADAPT=True,
          SCALP_SIDE_LOOKBACK=8, SCALP_SIDE_MIN_TRADES=4, SCALP_SIDE_PENALTY=10, SCALP_DELTA_MIN=0.55, SCALP_DELTA_MAX=0.70,
          SCALP_V2_STRICT=False, AGENTS=False)


def base_cfg(preset, **over):
    """config.py values + V2 rule defaults from the spec + a preset (LEGACY or V2) + overrides, in that order."""
    c = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    c.update(V.DEFAULTS)
    c.update(preset)
    c.update(over)
    return c


def frame(df, vol=None):
    x = pd.DataFrame(dict(o=df.Open, h=df.High, l=df.Low, c=df.Close)).astype(float)
    if vol is not None:
        x["v"] = vol.reindex(x.index)
    return x


class Market:
    """Data + cached per-index candles/features/legacy signals for one data download."""

    def __init__(self, data, names):
        self.data, self.names = data, names
        self.x = {n: frame(data["idx"][n], data["vol"]) for n in names}
        self.other = {"NIFTY": frame(data["bank"]), "SENSEX": frame(data["idx"]["NIFTY"])}
        self.vix_s = pd.Series(data["vix"])
        self.vdays = sorted(data["vix"])
        self.days = sorted(set().union(*[set(self.x[n].index.date) for n in names]))[1:]   # day 1 = warm-up
        self._legacy, self._feat = {}, {}
        self.atr_med = None

    def iv(self, day):
        prev = [d for d in self.vdays if d < day]
        return self.data["vix"][prev[-1]] if prev else 0.13

    def feats(self, n, cfg):
        keys = ("SCALP_EMA_SLOPE_LOOKBACK", "SCALP_PULLBACK_LOOKBACK", "SCALP_PULLBACK_TOL_PCT", "SCALP_BREAKOUT_LOOKBACK",
                "SCALP_VOL_MULT", "SCALP_CLOSE_POS_MIN", "SCALP_RSI_PERIOD", "SCALP_VOL_AVG_N", "SCALP_ADX_MIN",
                "SCALP_ATR_MIN_PCT", "SCALP_EMA_SEPARATION_MIN", "SCALP_CHOP_FILTER", "SCALP_CHOP_MAX", "SCALP_MAX_EMA_CROSSES",
                "SCALP_HIGHVOL_MULT", "SCALP_MAX_OPENING_RANGE_PCT", "VIX_PERCENTILE_LOOKBACK")
        k = (n,) + tuple(V.P(cfg, x) for x in keys)
        if k not in self._feat:
            self._feat[k] = V.features(self.x[n], cfg, self.vix_s, self.other[n])
        return self._feat[k]

    def legacy(self, n):
        """Current scalper's signal per minute (strategy.signal_v2 on 1-min candles), computed once."""
        if n not in self._legacy:
            cfg = base_cfg(LEGACY)
            cfg.update(MIN_ATR_PCT=LEGACY["SCALP_MIN_ATR_PCT"], RR=2.0, SCALP_STRATEGY="ema")
            hist, out = [], {}
            for ts, r in self.x[n].iterrows():
                hist.append(dict(o=r.o, h=r.h, l=r.l, c=r.c, v=r.get("v"), day=ts.date()))
                hist = hist[-400:]
                s = strategy.signal_v2(hist, cfg)
                if s:
                    out[ts] = s["side"]
            self._legacy[n] = out
        return self._legacy[n]


def hm_of(ts, s):
    h, m = map(int, s.split(":"))
    return ts.replace(hour=h, minute=m, second=0)


def tod_bucket(t):
    m = t.hour * 60 + t.minute
    return "morning (<11:30)" if m < 690 else "midday (11:30–13:30)" if m < 810 else "afternoon (>13:30)"


# ---------------------------------------------------------------------------------------------- engine
def simulate(M, cfg, cost_key="base", days=None, lots=1, rejections=None):
    """One pass over the data. cfg: base_cfg(LEGACY or V2, **overrides). Returns trades DataFrame."""
    sc = C.SCENARIOS[cost_key]
    delay = sc.get("delay_bars", 0)
    legacy = cfg["MODE_"] == "legacy"
    allowed = set(days) if days is not None else set(M.days)
    ev = {}
    for n in M.names:
        if legacy:
            ev[n] = None
        else:
            F = M.feats(n, cfg)
            ev[n] = (F, V.evaluate(F, cfg, liquidity_score=7))   # no historical quotes: liquidity "unknown" = 7/10
    timeline = sorted(set().union(*[set(M.x[n].index) for n in M.names]))
    rows = {n: M.x[n] for n in M.names}
    # plain-dict lookups: pandas .loc inside a 7,000-row loop is the slow part
    rec = {n: M.x[n][["o", "h", "l", "c"]].to_dict("index") for n in M.names}
    erec, frec = {}, {}
    for n in M.names:
        if ev[n] is not None:
            F, E = ev[n]
            erec[n] = E.to_dict("index")
            frec[n] = F[["c", "ema9", "atr", "atr_pct", "vix_pctl"]].to_dict("index")
    sq_off = config.SQUARE_OFF_AT
    trades, pos, pending, dstate = [], {}, {}, {}
    last_exit, last_loss = {}, {}        # index -> ts ; (index, side) -> ts of a losing exit
    reset_seen = {}                      # (index, side) -> price has closed back through EMA9 since the loss
    completed = []                       # (side, net) of closed trades, for side adaptation
    atr_med = float(np.nanmedian(np.concatenate([M.feats(n, cfg).atr_pct.values for n in M.names]))) if not legacy else \
        float(np.nanmedian(np.concatenate([(M.x[n].h - M.x[n].l).values / M.x[n].c.values * 100 for n in M.names])))

    def prem(p, ts, level):
        return V.bs(level, p["strike"], max((p["exp"] - ts).total_seconds(), 60) / (365 * 24 * 3600), p["iv"], p["side"])[0]

    def close(n, ts, level, px_mid, reason):
        p = pos.pop(n)
        sell = C.fill(max(0.05, px_mid), "sell", sc)
        gross = (sell - p["buy"]) * p["qty"]
        ch = C.charges(p["buy"] * p["qty"], sell * p["qty"], sc)
        net = gross - ch
        st = dstate[ts.date()]
        st["pnl"] += net; st["n"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -cfg["SCALP_MAX_DAILY_LOSS"] or st["consec"] >= cfg["SCALP_MAX_CONSECUTIVE_LOSSES"]
                or st["n"] >= cfg["SCALP_MAX_TRADES_PER_DAY"]):
            st["locked"] = True
        last_exit[n] = ts
        if net < 0:
            last_loss[(n, p["side"])] = ts; reset_seen[(n, p["side"])] = False
        completed.append((p["side"], net))
        trades.append(dict(date=ts.date(), index=n, side=p["side"], entry_type=p["etype"], regime=p["regime"],
                           entry_time=p["t"].strftime("%H:%M"), exit_time=ts.strftime("%H:%M"),
                           hold_min=int((ts - p["t"]).total_seconds() // 60), strike=p["strike"], delta=round(p["delta"], 2),
                           idx_entry=round(p["idx"], 1), idx_exit=round(level, 1), entry=round(p["buy"], 2), exit=round(sell, 2),
                           sl_pts=round(p["sl"], 2), qty=p["qty"], gross=round(gross), charges=round(ch), net=round(net),
                           R=net / (p["sl"] * p["qty"]), reason=reason, score=p["score"], tod=tod_bucket(p["t"]),
                           vol="high vol" if p["atr_pct"] >= atr_med else "low vol", vix_pctl=p["vix_pctl"]))

    def reject(ts, n, side, why, score=None, comps=None, regime=None):
        if rejections is not None:
            rejections.append(dict(date=ts.date(), time=ts.strftime("%H:%M"), index=n, signal=side, score=score,
                                   regime=regime, reason=why, **(comps or {})))

    def open_pos(n, ts, level, p, iv_day):
        p["iv"] = iv_day
        mid = prem(p, ts, level)
        p["buy"] = C.fill(mid, "buy", sc)
        p["cost_unit"] = strategy.cost_per_unit(p["buy"], p["qty"])
        p["stop"], p["target"] = p["buy"] - p["sl"], p["buy"] + p["tg"]
        p["best"], p["be"], p["t"], p["idx"] = p["buy"], False, ts, level
        pos[n] = p

    for ts in timeline:
        for n in M.names:
            c = rec[n].get(ts)
            if c is None:
                continue
            day = ts.date()
            st = dstate.setdefault(day, dict(pnl=0.0, n=0, consec=0, locked=False))
            close_ts = ts + timedelta(minutes=1)
            # delayed entry (conservative fills): enter at this candle's open
            if n in pending:
                p = pending.pop(n)
                if n not in pos and not st["locked"] and ts < hm_of(ts, sq_off):
                    open_pos(n, ts, c["o"], p, M.iv(day))
            if n in pos:
                p = pos[n]
                if ts >= hm_of(ts, sq_off):
                    close(n, ts, c["o"], prem(p, ts, c["o"]), "square-off")
                else:
                    up = p["side"] == "CE"
                    worst_i, best_i = (c["l"], c["h"]) if up else (c["h"], c["l"])
                    p_open = prem(p, ts, c["o"])
                    lo_p, hi_p = prem(p, close_ts, worst_i), prem(p, close_ts, best_i)
                    stop_name = "stop at cost" if p["be"] else "stop-loss"
                    if p_open <= p["stop"] and ts > p["t"]:                       # gapped through the stop
                        close(n, ts, c["o"], p_open, stop_name)
                    elif lo_p <= p["stop"]:
                        close(n, close_ts, worst_i, p["stop"], stop_name)
                    elif hi_p >= p["target"]:
                        close(n, close_ts, best_i, p["target"], "target")
                    else:
                        p["best"] = max(p["best"], hi_p)
                        if not p["be"] and p["be_at"] and p["best"] - p["buy"] >= p["be_at"]:
                            p["stop"], p["be"] = p["buy"] + p["cost_unit"], True
                        if cfg["SCALP_MAX_HOLD_MIN"] and (close_ts - p["t"]).total_seconds() >= cfg["SCALP_MAX_HOLD_MIN"] * 60:
                            close(n, close_ts, c["c"], prem(p, close_ts, c["c"]), "time stop")
            # ---- entries on candle close
            if legacy:
                side = M.legacy(n).get(ts, "")
                E = F = None
                trig = side
            else:
                e = erec[n][ts]
                side, trig = e["side"], e["trigger_side"]
                if (n, "CE") in last_loss or (n, "PE") in last_loss:
                    fr = frec[n][ts]
                    for sd, s in (("CE", 1), ("PE", -1)):
                        if (n, sd) in last_loss and ts > last_loss[(n, sd)] and (fr["c"] - fr["ema9"]) * s < 0:
                            reset_seen[(n, sd)] = True
            if not trig or day not in allowed:
                continue
            comps = None if legacy else {k: e["s_" + k] for k in V.WEIGHTS}
            info = dict(score=None if legacy else e["score"], comps=comps, regime=None if legacy else e["regime"])
            if not side:
                reject(ts, n, trig, e["reason"], **info); continue
            why = None
            if n in pos or pos or n in pending:
                why = "position already open"
            elif st["locked"]:
                why = "daily risk lock"
            elif not (hm_of(ts, cfg["SCALP_NO_ENTRY_BEFORE"]) <= close_ts <= hm_of(ts, cfg["SCALP_NO_ENTRY_AFTER"])):
                why = "outside entry window"
            elif n in last_exit and (close_ts - last_exit[n]).total_seconds() < cfg["SCALP_COOLDOWN_MIN"] * 60:
                why = "cooldown"
            elif not legacy and cfg["SCALP_STRUCTURAL_COOLDOWN"] and (n, side) in last_loss and last_loss[(n, side)].date() == day:
                lb = max(V.P(cfg, "SCALP_PULLBACK_LOOKBACK"), V.P(cfg, "SCALP_BREAKOUT_LOOKBACK"))
                if not reset_seen.get((n, side)) or ts - timedelta(minutes=lb) <= last_loss[(n, side)]:
                    why = "structural cooldown (no new setup since the stop)"
            if not why and not legacy:
                pen = V.side_penalty(completed, side, cfg)
                if pen and e["score"] < cfg["TRADE_SCORE_MIN"] + pen:
                    why = f"{side} side under-performing (needs +{pen} score)"
            if not why and cfg["AGENTS"]:
                ch = crude_change(M.data["brent"], close_ts)
                veto = strategy.agent_veto(side, {"change_pct": ch} if ch is not None else None, None, cfg)
                if veto:
                    why = veto
            if why:
                reject(ts, n, side, why, **info); continue
            spec = config.INSTRUMENT_SPECS[n]
            qty = spec["lot"] * lots
            exp = next_expiry(close_ts, spec["expiry_weekday"])
            iv = M.iv(day)
            T = (exp - close_ts).total_seconds() / (365 * 24 * 3600)
            if legacy:
                strike = strategy.itm_strike(c["c"], side, spec["step"], cfg["ITM_STRIKES"])
                delta = V.bs(c["c"], strike, T, iv, side)[1]
                sl, tg, be_at = cfg["SCALP_SL_POINTS"], cfg["SCALP_TARGET_POINTS"], cfg["SCALP_BE_AT_POINTS"]
                etype, regime, score, atr_pct, vixp = "ema-cross", "", None, (c["h"] - c["l"]) / c["c"] * 100, None
                if V.bs(c["c"], strike, T, iv, side)[0] * qty > config.MAX_PREMIUM_PER_TRADE * lots:
                    reject(ts, n, side, "premium above cap"); continue
            else:
                step = spec["step"]
                atm = round(c["c"] / step) * step
                cands = []
                for k in range(0, 4):
                    strike = atm - k * step if side == "CE" else atm + k * step
                    pr, dl = V.bs(c["c"], strike, T, iv, side)
                    cands.append(dict(strike=strike, delta=dl, premium=pr, itm_steps=k))
                best, liq, why = V.score_options(cands, cfg, config.MAX_PREMIUM_PER_TRADE * lots / qty)
                if not best:
                    reject(ts, n, side, "option filter: " + why, **info); continue
                strike, delta = best["strike"], best["delta"]
                fr = frec[n][ts]
                sl = V.premium_stop(fr["atr"], delta, cfg)
                tg = cfg["SCALP_RR"] * sl
                be_at = cfg["SCALP_BE_AT_R"] * sl if cfg["SCALP_BE_AT_R"] else 0
                etype, regime, score, atr_pct, vixp = e["entry_type"], e["regime"], int(e["score"]), fr["atr_pct"], fr["vix_pctl"]
            worst = qty * (sl + strategy.cost_per_unit(V.bs(c["c"], strike, T, iv, side)[0], qty))
            if worst > cfg["SCALP_MAX_DAILY_LOSS"] * lots + st["pnl"]:
                reject(ts, n, side, "loss at stop exceeds today's remaining risk", **info); continue
            p = dict(side=side, strike=strike, delta=delta, exp=exp, qty=qty, sl=sl, tg=tg, be_at=be_at, etype=etype,
                     regime=regime, score=score, atr_pct=atr_pct, vix_pctl=vixp)
            if delay:
                pending[n] = p
            else:
                open_pos(n, close_ts, c["c"], p, iv)
    for n in list(pos):
        last = rows[n].index[-1]
        close(n, last, rows[n].c.iloc[-1], prem(pos[n], last, rows[n].c.iloc[-1]), "end of data")
    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------------------------- metrics
def metrics(t):
    if t is None or not len(t):
        return dict(trades=0, wins=0, losses=0, win_rate=0, gross=0, charges=0, net=0, pf=0, avg_trade=0, expectancy_R=0,
                    max_dd=0, worst_day=0, avg_hold=0, avg_win=0, avg_loss=0, largest_win=0, largest_loss=0, max_consec_losses=0)
    w, l = t[t.net > 0], t[t.net <= 0]
    eq = t.net.cumsum()
    streak = best = 0
    for x in t.net:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    return dict(trades=len(t), wins=len(w), losses=len(l), win_rate=round(100 * len(w) / len(t), 1), gross=int(t.gross.sum()),
                charges=int(t.charges.sum()), net=int(t.net.sum()),
                pf=round(w.net.sum() / -l.net.sum(), 2) if len(l) and l.net.sum() < 0 else float("inf"),
                avg_trade=int(t.net.mean()), expectancy_R=round(t.R.mean(), 3),
                max_dd=int((eq - eq.cummax().clip(lower=0)).min()), worst_day=int(t.groupby("date").net.sum().min()),
                avg_hold=round(t.hold_min.mean(), 1), avg_win=int(w.net.mean()) if len(w) else 0,
                avg_loss=int(l.net.mean()) if len(l) else 0, largest_win=int(t.net.max()), largest_loss=int(t.net.min()),
                max_consec_losses=best)


def table(rows):
    return pd.DataFrame(rows).T


def breakdown(t, key):
    if t is None or not len(t):
        return pd.DataFrame()
    out = {}
    for k, g in t.groupby(key):
        m = metrics(g)
        out[k if not isinstance(k, tuple) else " ".join(k)] = {x: m[x] for x in ("trades", "win_rate", "pf", "net", "avg_trade", "max_dd")}
    return pd.DataFrame(out).T


def fills(M, cfg, days=None, rej=None):
    res = {}
    for lab, key in FILLS.items():
        res[lab] = simulate(M, cfg, key, days, rejections=rej if key == "base" else None)
    return res


def obj(M, cfg, days):
    """Selection objective on given days: mean of the SQN/recovery objective under base and conservative fills.
    Never total P&L."""
    o = []
    for key in ("base", "conservative"):
        t = simulate(M, cfg, key, days)
        o.append(objective(t, min_trades=max(3, len(days) // 3)) if len(t) else -9.0)
    return float(np.mean(o))


GRID = [  # one parameter group at a time (spec section 29)
    ("ADX_MIN", [dict(SCALP_ADX_MIN=v) for v in (18, 20, 22, 25)]),
    ("TRADE_SCORE_MIN", [dict(TRADE_SCORE_MIN=v) for v in (70, 75, 80)]),
    ("MAX_VWAP_DISTANCE", [dict(SCALP_MAX_VWAP_DISTANCE_PCT=v) for v in (0.08, 0.10, 0.12, 0.15)]),
    ("RSI CE", [dict(SCALP_CE_RSI_MIN=a, SCALP_CE_RSI_MAX=b) for a, b in ((52, 65), (52, 68), (55, 68))]),
    ("RSI PE", [dict(SCALP_PE_RSI_MIN=a, SCALP_PE_RSI_MAX=b) for a, b in ((32, 48), (35, 48), (32, 45))]),
    ("PULLBACK", [dict(SCALP_PULLBACK_ENTRY=v) for v in (True, False)]),
    ("MACD", [dict(SCALP_USE_MACD=v) for v in (True, False)]),
]


def tune(M, start, days):
    """Coordinate search (one group at a time) on `days` only. Returns the chosen overrides."""
    best = {}
    for _, opts in GRID:
        scored = [(obj(M, base_cfg(V2, **{**best, **o}), days), o) for o in opts]
        best.update(max(scored, key=lambda x: x[0])[1])
    return best


def walk_forward(M, train=8, val=4, test=3, step=3):
    days, folds = M.days, []
    i = 0
    while i + train + val + test <= len(days):
        folds.append((days[i:i + train], days[i + train:i + train + val], days[i + train + val:i + train + val + test]))
        i += step
    rows, oos = [], defaultdict(list)
    for k, (tr, va, te) in enumerate(folds, 1):
        tuned = tune(M, V2, tr)
        cand = {"defaults": {}, "tuned on train": tuned}
        pick = max(cand, key=lambda c: obj(M, base_cfg(V2, **cand[c]), va))   # validation decides
        chosen = cand[pick]
        for lab, cfg in (("current scalper", base_cfg(LEGACY)), ("V2 chosen", base_cfg(V2, **chosen)),
                         ("V2 defaults", base_cfg(V2))):
            for fl in ("base", "conservative"):
                t = simulate(M, cfg, fl, te)
                oos[(lab, fl)].append(t)
                m = metrics(t)
                rows.append(dict(fold=k, test_days=f"{te[0]}→{te[-1]}", config=lab, fills=fl, picked=pick if lab == "V2 chosen" else "",
                                 overrides=str(chosen) if lab == "V2 chosen" else "", trades=m["trades"], net=m["net"], pf=m["pf"],
                                 exp_R=m["expectancy_R"], max_dd=m["max_dd"]))
    combined = {f"{lab} · {fl}": metrics(pd.concat([t for t in ts if len(t)]) if any(len(t) for t in ts) else None)
                for (lab, fl), ts in oos.items()}
    return folds, pd.DataFrame(rows), table(combined)


# ---------------------------------------------------------------------------------------------- report
def fmt(df):
    return df.to_markdown() if len(df) else "_no trades_"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="BOTH", choices=["NIFTY", "SENSEX", "BOTH"])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    names = ["NIFTY", "SENSEX"] if a.instrument == "BOTH" else [a.instrument]
    t0 = time.time()
    M = Market(load(a.refresh), names)
    os.makedirs(OUT, exist_ok=True)
    L = []
    say = lambda s="": (print(s), L.append(s))
    say(f"# Scalper V2 vs current scalper\n")
    say(f"{', '.join(names)} · {len(M.days)} trading days ({M.days[0]} → {M.days[-1]}) · 1 lot · same 1-min data, "
        f"option model, fills and engine for every test.\n")

    tests = {"A current scalper": base_cfg(LEGACY), "B V2 strict (spec-literal)": base_cfg(V2, SCALP_V2_STRICT=True),
             "C V2 no agents": base_cfg(V2), "D V2 + agents (crude)": base_cfg(V2, AGENTS=True)}
    res, rej = {}, {}
    for lab, cfg in tests.items():
        rej[lab] = []
        res[lab] = fills(M, cfg, rej=rej[lab])
    say("## 1. Headline — every test under all three fill models\n")
    head = {}
    for lab in tests:
        for fl in FILLS:
            head[f"{lab} · {fl}"] = metrics(res[lab][fl])
    say(fmt(table(head)[["trades", "win_rate", "net", "pf", "avg_trade", "expectancy_R", "max_dd", "worst_day", "charges"]]))
    say("\n## 2. Full metrics, base fills\n")
    say(fmt(table({lab: metrics(res[lab]["base"]) for lab in tests})))

    A, Cb = res["A current scalper"]["base"], res["C V2 no agents"]["base"]
    for lab, t in (("current scalper (A)", A), ("V2 (C)", Cb)):
        say(f"\n## 3. Diagnostics — {lab}, base fills\n")
        for key, title in ((["index", "side"], "index × side"), ("side", "CE vs PE"), ("index", "NIFTY vs SENSEX"),
                           ("tod", "time of day"), ("vol", "volatility (1-min ATR% vs median)"),
                           ("entry_type", "entry type"), ("regime", "regime at entry"), ("reason", "exit reason")):
            if len(t) and (key not in ("regime",) or t[key].astype(bool).any()):
                say(f"**{title}**\n\n" + fmt(breakdown(t, key)) + "\n")

    # regime of the CURRENT scalper's trades (computed with V2's regime detector, entry minute)
    if len(A):
        reg = []
        for _, r in A.iterrows():
            F = M.feats(r["index"], base_cfg(V2))
            ts = F.index[(F.day == r["date"]) & (F.tod == int(r["entry_time"][:2]) * 60 + int(r["entry_time"][3:]) - 1)]
            reg.append(F.loc[ts[0]].regime if len(ts) else "?")
        A2 = A.assign(regime=reg, trend=["trend" if "TREND" in x else "range/high-vol" for x in reg])
        say("\n## 4. Where the current scalper loses (regime from V2's detector at entry)\n")
        say(fmt(breakdown(A2, "regime")) + "\n")
        say(fmt(breakdown(A2, ["index", "side", "trend"])) + "\n")
        say(fmt(breakdown(A2, ["side", "tod"])) + "\n")
        A2.to_csv(os.path.join(OUT, "trades_current.csv"), index=False)
    Cb.to_csv(os.path.join(OUT, "trades_v2.csv"), index=False)
    r = pd.DataFrame(rej["C V2 no agents"])
    if len(r):
        r.to_csv(os.path.join(OUT, "rejections_v2.csv"), index=False)
        say("\n## 5. Why V2 setups were rejected (base fills, setups with an entry trigger)\n")
        say(fmt(r.reason.value_counts().rename("count").to_frame().head(20)) + "\n")

    if not a.quick:
        say("\n## 6. Stop / target sizes (V2, fixed premium points vs dynamic)\n")
        g = {}
        for sl, tg in ((5, 10), (6, 12), (7, 14), (8, 16), (10, 20)):
            cfg = base_cfg(V2, SCALP_DYNAMIC_SL=False, SCALP_SL_POINTS=sl, SCALP_RR=tg / sl)
            for fl in ("base", "conservative"):
                g[f"{sl}/{tg} · {fl}"] = metrics(simulate(M, cfg, fl))
        for fl in ("base", "conservative"):
            g[f"dynamic (ATR×delta×0.8, 5–12) · {fl}"] = metrics(simulate(M, base_cfg(V2), fl))
        say(fmt(table(g)[["trades", "win_rate", "net", "pf", "expectancy_R", "max_dd"]]))
        say("\n## 7. Break-even (stop to cost) trigger\n")
        g = {}
        for be in (0, 1.0, 1.2, 1.5):
            for fl in ("base", "conservative"):
                t = simulate(M, base_cfg(V2, SCALP_BE_AT_R=be), fl)
                m = metrics(t)
                m["stop_at_cost"] = int((t.reason == "stop at cost").sum()) if len(t) else 0
                g[f"BE {'off' if not be else f'{be}R'} · {fl}"] = m
        say(fmt(table(g)[["trades", "win_rate", "net", "pf", "expectancy_R", "max_dd", "stop_at_cost"]]))
        say("\n## 8. One-at-a-time sensitivity on all days (descriptive only — NOT used to pick settings)\n")
        g = {}
        for name, opts in GRID:
            for o in opts:
                for fl in ("base", "conservative"):
                    m = metrics(simulate(M, base_cfg(V2, **o), fl))
                    g[f"{name} {list(o.values())} · {fl}"] = m
        say(fmt(table(g)[["trades", "win_rate", "net", "pf", "expectancy_R", "max_dd"]]))
        say("\n## 9. Walk-forward (train 8 days → validate 4 → test 3, step 3)\n")
        folds, wf, comb = walk_forward(M)
        say(f"{len(folds)} folds; out-of-sample days in total: {sum(len(f[2]) for f in folds)}.\n")
        say(fmt(wf.set_index(["fold", "config", "fills"])) + "\n")
        say("**Combined out-of-sample**\n\n" + fmt(comb[["trades", "win_rate", "net", "pf", "expectancy_R", "max_dd"]]))
    say(f"\n_Runtime {time.time() - t0:.0f}s. Trades: reports/scalper_v2/trades_*.csv, rejections: rejections_v2.csv._")
    with open(os.path.join(OUT, "SCALPER_V2_RESULTS.md"), "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
