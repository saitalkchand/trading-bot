"""
Lab backtest engine for the NIFTY options scalper. Paper only — it never imports the broker.

Same market model as scalp_backtest.py (1-min NIFTY candles from Yahoo, Black-Scholes ITM option premiums with
IV = previous day's India VIX, v3.costs fill scenarios and charges, the scalper's daily limits), so results are
comparable. Two modes:

  compat=True   reproduces scalp_backtest.run() trade-for-trade (used to prove the baseline is reproducible)
  compat=False  stricter execution, used for every lab experiment:
                * an entry signalled on a candle fills at that close (base) or the NEXT candle's open
                  (conservative: delay_bars=1) — never earlier
                * a candle that opens beyond the stop exits at that open, not at the stop
                * a target fills only if the premium trades THROUGH it by TARGET_THROUGH points (a touch isn't a fill)
                * stop before target when a candle could have hit both
                * stops/targets rounded to the 0.05 tick; every change audited (exits.py)

Decisions use only completed candles: entries are evaluated after a candle closes, exits are checked on the
following candles with the levels set before them.
"""
import math
from datetime import timedelta

import pandas as pd

import config
import strategy as legacy
from backtest import bs_price, next_expiry, years_to
from scalp_backtest import crude_change
from scalp_v2 import bs as bs_delta
from v3 import costs as C

from . import exits as X
from . import regime as RG
from .features import Features
from .sizing import lots_for_risk
from .strategies import STRATEGIES

# the scalper's limits as configured when this lab was built — pinned so later Settings edits can't move results
RISK = dict(SCALP_MAX_DAILY_LOSS=1000, SCALP_MAX_TRADES_PER_DAY=6, SCALP_MAX_CONSECUTIVE_LOSSES=2, SCALP_COOLDOWN_MIN=5,
            SCALP_NO_ENTRY_BEFORE="09:35", SCALP_NO_ENTRY_AFTER="14:30", SCALP_MAX_HOLD_MIN=10, SQUARE_OFF_AT="15:10",
            MAX_PREMIUM_PER_TRADE=40000, ITM_STRIKES=1, LOTS=1, USE_CRUDE_AGENT=True, CRUDE_BLOCK_PCT=1.0,
            USE_NEWS_AGENT=False, NEWS_BLOCK_ON_EVENT=False, NEWS_BLOCK_SCORE=0.4)
# the volume rule's own settings (strategy.signal_volume reads these)
VOLUME_RULE = dict(SCALP_VOL_MULT=1.5, SCALP_VOL_AVG_N=20, SCALP_VOL_BREAKOUT_N=5, EMA_SLOW=21, MIN_ATR_PCT=0.02, RR=2.0)
# settings the reproduction check pins the OLD engine (scalp_backtest.py reads config live) to, so it runs the same
# 5/10 baseline as engine.RISK + exits.EXIT_DEFAULTS whatever the scalper is configured to today
LEGACY_SCALP = dict(SCALP_SL_POINTS=5, SCALP_TARGET_POINTS=10, SCALP_BE_AT_POINTS=5, SCALP_POINTS_ON="premium",
                    SCALP_MAX_DAILY_LOSS=1000, SCALP_MAX_TRADES_PER_DAY=6, SCALP_MAX_CONSECUTIVE_LOSSES=2,
                    SCALP_COOLDOWN_MIN=5, SCALP_NO_ENTRY_BEFORE="09:35", SCALP_NO_ENTRY_AFTER="14:30",
                    SCALP_MAX_HOLD_MIN=10, SCALP_LOTS=1, SQUARE_OFF_AT="15:10", ITM_STRIKES=1, MAX_PREMIUM_PER_TRADE=40000,
                    SCALP_MIN_ATR_PCT=0.02, SCALP_VOL_MULT=1.5, SCALP_VOL_AVG_N=20, SCALP_VOL_BREAKOUT_N=5)


class legacy_config:
    """with legacy_config(): scalp_backtest.run(...) — temporarily sets config to the pinned baseline, then restores."""

    def __enter__(self):
        self.saved = {k: getattr(config, k) for k in LEGACY_SCALP}
        for k, v in LEGACY_SCALP.items():
            setattr(config, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(config, k, v)


TARGET_THROUGH = 0.5     # premium points beyond the target needed for a limit target to count as filled


def tod_bucket(t):
    m = t.hour * 60 + t.minute
    return "morning (<11:30)" if m < 690 else "midday (11:30–13:30)" if m < 810 else "afternoon (>13:30)"


class Market:
    """NIFTY 1-min candles + NIFTYBEES volume + VIX + Brent from a frozen snapshot (see data.py)."""

    def __init__(self, data, name="NIFTY"):
        df = data["idx"][name]
        self.name, self.spec = name, config.INSTRUMENT_SPECS[name]
        vol = data["vol"].reindex(df.index)
        self.bars = [dict(t=ts, o=float(r.Open), h=float(r.High), l=float(r.Low), c=float(r.Close),
                          v=None if (v != v) else float(v))
                     for ts, r, v in zip(df.index, df.itertuples(), vol.values)]
        self.vix, self.brent = data["vix"], data["brent"]
        self.vdays = sorted(self.vix)
        self.days = sorted({b["t"].date() for b in self.bars})
        self.trade_days = self.days[1:]          # day 1 only warms the indicators up

    def iv(self, day):
        prev = [d for d in self.vdays if d < day]
        return self.vix[prev[-1]] if prev else 0.13


def _at(ts, s):
    h, m = map(int, s.split(":"))
    return ts.replace(hour=h, minute=m, second=0)


def run(M, exp, fills="base", days=None, compat=False, collect_setups=False):
    """exp: dict(strategy=<key in STRATEGIES>, strat_cfg={}, regime=None|{cfg}, exit={cfg}, risk={overrides},
    lots=1, agents=True). Returns dict(trades=DataFrame, rejections=list, setups=list)."""
    sc = C.SCENARIOS[fills]
    delay = 0 if compat else sc.get("delay_bars", 0)
    risk = dict(RISK, **exp.get("risk", {}))
    ecfg = dict(X.EXIT_DEFAULTS, **exp.get("exit", {}))
    scfg = dict(VOLUME_RULE, **exp.get("strat_cfg", {}))
    lots = exp.get("lots", risk["LOTS"])
    allowed = set(days) if days is not None else set(M.trade_days)
    F = Features()
    det = STRATEGIES[exp["strategy"]](scfg)
    spec = M.spec
    trades, rejections = [], []
    pos, pending, dstate, last_exit = None, None, {}, None

    def prem(p, ts, level):
        return bs_price(level, p["strike"], years_to(p["exp"], ts), p["iv"], p["side"])

    def reject(ts, sig, why):
        rejections.append(dict(t=ts, date=ts.date(), side=sig["side"], kind=sig["kind"], reason=why))

    def close(ts, level, px_mid, reason):
        nonlocal pos, last_exit
        p, pos = pos, None
        sell = C.fill(max(0.05, px_mid), "sell", sc)
        gross = (sell - p["buy"]) * p["qty"]
        ch = C.charges(p["buy"] * p["qty"], sell * p["qty"], sc)
        net = gross - ch
        gross_mid = (max(0.05, px_mid) - p["mid"]) * p["qty"]
        st = dstate[ts.date()]
        st["pnl"] += net; st["n"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -risk["SCALP_MAX_DAILY_LOSS"] or st["consec"] >= risk["SCALP_MAX_CONSECUTIVE_LOSSES"]
                or st["n"] >= risk["SCALP_MAX_TRADES_PER_DAY"]):
            st["locked"] = True
        last_exit = ts
        trades.append(dict(date=ts.date(), side=p["side"], kind=p["kind"], entry_time=p["t"].strftime("%H:%M"),
                           exit_time=ts.strftime("%H:%M"), hold_min=int((ts - p["t"]).total_seconds() // 60),
                           strike=p["strike"], delta=round(p["delta"], 2), idx_entry=round(p["idx"], 1),
                           idx_exit=round(level, 1), idx_stop=p.get("idx_stop"), idx_target=p.get("idx_target"),
                           entry=round(p["buy"], 2), exit=round(sell, 2), entry_mid=round(p["mid"], 2),
                           exit_mid=round(px_mid, 2), sl_pts=round(p["sl"], 2), tg_pts=round(p["tg"], 2), qty=p["qty"],
                           lots=p["lots"], gross=round(gross, 2), charges=round(ch, 2),
                           slippage=round(gross_mid - gross, 2), net=round(net, 2), R=net / (p["sl"] * p["qty"]),
                           reason=reason, regime=p["regime"], tod=tod_bucket(p["t"]), rvol=p["rvol"],
                           why=p["why"], t_sig=p["t_sig"], t_entry=p["t"], t_exit=ts, events=p["events"],
                           sig_range_atr=p["sig_range_atr"], vwap_dist_pct=p["vwap_dist_pct"]))

    def open_pos(ts, level, p):
        nonlocal pos
        p["mid"] = prem(p, ts, level)
        p["buy"] = C.fill(p["mid"], "buy", sc)
        p["cost_unit"] = legacy.cost_per_unit(p["buy"], p["qty"])
        p["t"], p["idx"] = ts, level
        p["round"] = not compat
        why = X.initial(p, p["sig"], dict(buy=p["buy"], delta=p["delta"], idx_atr=p["atr"], cost_unit=p["cost_unit"]), ecfg)
        if why:
            reject(ts, p["sig"], why)
            return
        pos = p

    for c in M.bars:
        ts = c["t"]
        day = ts.date()
        st = dstate.setdefault(day, dict(pnl=0.0, n=0, consec=0, locked=False))
        close_ts = ts + timedelta(minutes=1)
        if pending is not None:                        # conservative: fill at this candle's open
            p, pending = pending, None
            if pos is None and not st["locked"] and ts < _at(ts, risk["SQUARE_OFF_AT"]) and p["t_sig"].date() == day:
                open_pos(ts, c["o"], p)
        exited = False
        if pos is not None and ts >= pos["t"]:
            p = pos
            if ts >= _at(ts, risk["SQUARE_OFF_AT"]):
                close(ts, c["o"], prem(p, ts, c["o"]), "square-off"); exited = True
            else:
                up = p["side"] == "CE"
                worst_i, best_i = (c["l"], c["h"]) if up else (c["h"], c["l"])
                lo_p, hi_p = prem(p, close_ts, worst_i), prem(p, close_ts, best_i)
                p_open = prem(p, ts, c["o"])
                stop_name = "stop at cost" if p["be"] else "stop-loss"
                if p["events"] and p["stop"] > p["buy"] + p["cost_unit"] + 1e-9:
                    stop_name = "trailing/locked stop"
                through = 0 if compat else TARGET_THROUGH
                if not compat and ts > p["t"] and p_open <= p["stop"]:
                    close(ts, c["o"], p_open, stop_name + " (gap)"); exited = True
                elif lo_p <= p["stop"]:
                    close(close_ts, worst_i, p["stop"], stop_name); exited = True
                elif hi_p >= p["target"] + through:
                    close(close_ts, best_i, p["target"], "target" + (" (extended)" if p["ext"] else "")); exited = True
        F.update(c)
        sig = det.update(F)
        if pos is not None and not exited and ts >= pos["t"]:
            p = pos
            up = p["side"] == "CE"
            hi_p = prem(p, close_ts, c["h"] if up else c["l"])
            X.update(p, hi_p, prem(p, close_ts, c["c"]), F, close_ts, ecfg)
            if risk["SCALP_MAX_HOLD_MIN"] and (close_ts - p["t"]).total_seconds() >= risk["SCALP_MAX_HOLD_MIN"] * 60:
                close(close_ts, c["c"], prem(p, close_ts, c["c"]), "time stop")
        # ---- entry decision on this candle's close
        if not sig or day not in allowed:
            continue
        why = None
        if pos is not None or pending is not None:
            why = "position already open"
        elif st["locked"]:
            why = "daily risk lock"
        elif not (_at(ts, risk["SCALP_NO_ENTRY_BEFORE"]) <= close_ts <= _at(ts, risk["SCALP_NO_ENTRY_AFTER"])):
            why = "outside entry window"
        elif last_exit is not None and (close_ts - last_exit).total_seconds() < risk["SCALP_COOLDOWN_MIN"] * 60:
            why = "cooldown"
        elif exp.get("regime") is not None:
            why = RG.no_trade(F, sig["side"], exp["regime"])
        if not why and exp.get("agents", True):
            ch = crude_change(M.brent, close_ts)
            why = legacy.agent_veto(sig["side"], {"change_pct": ch} if ch is not None else None, None, risk)
        if why:
            reject(ts, sig, why); continue
        side = sig["side"]
        exp_dt = next_expiry(close_ts, spec["expiry_weekday"])
        iv = M.iv(day)
        strike = legacy.itm_strike(c["c"], side, spec["step"], risk["ITM_STRIKES"])
        T = years_to(exp_dt, close_ts)
        mid = bs_price(c["c"], strike, T, iv, side)
        delta = bs_delta(c["c"], strike, T, iv, side)[1]
        qty = spec["lot"] * lots
        if mid * qty > risk["MAX_PREMIUM_PER_TRADE"]:
            reject(ts, sig, "premium above cap"); continue
        # money at risk vs what's left of today's loss limit — sized down or rejected, never up
        buy_est = C.fill(mid, "buy", sc)
        probe = dict(side=side, qty=qty, round=not compat)
        bad = X.initial(probe, sig, dict(buy=buy_est, delta=delta, idx_atr=F.atr, cost_unit=0), ecfg)
        if bad:
            reject(ts, sig, bad); continue
        room = risk["SCALP_MAX_DAILY_LOSS"] + st["pnl"]
        n_lots, size_why = lots_for_risk(probe["sl"], legacy.cost_per_unit(buy_est, qty), spec["lot"], lots, room)
        if n_lots == 0:
            reject(ts, sig, "loss at stop exceeds today's remaining risk (" + size_why + ")"); continue
        qty = spec["lot"] * n_lots
        reg = RG.classify(F)[0]
        p = dict(side=side, kind=sig["kind"], strike=strike, delta=delta, exp=exp_dt, iv=iv, qty=qty, lots=n_lots,
                 sig=sig, atr=F.atr, regime=reg, rvol=round(F.rvol, 2) if F.rvol else None, why=sig["why"],
                 idx_stop=round(sig["idx_stop"], 1) if sig.get("idx_stop") else None,
                 idx_target=round(sig["idx_target"], 1) if sig.get("idx_target") else None, t_sig=ts,
                 sig_range_atr=round((c["h"] - c["l"]) / F.atr, 2), vwap_dist_pct=round((c["c"] - F.vwap) / F.vwap * 100, 3))
        if delay:
            pending = p
        else:
            open_pos(close_ts, c["c"], p)
    if pos is not None:
        last = M.bars[-1]
        close(last["t"], last["c"], prem(pos, last["t"], last["c"]), "end of data")
    return dict(trades=pd.DataFrame(trades), rejections=rejections, setups=det.log if collect_setups else [])
