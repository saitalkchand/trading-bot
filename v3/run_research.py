"""
Strategy V3 research — runs every experiment and writes reports/v3/results.md plus CSVs.

    .venv/bin/python -m v3.run_research            (≈10–20 min; news headlines are cached after the first run)

Two datasets:
  5m  : the real strategy timeframe, ~55 trading days (Yahoo's limit) — small sample
  60m : the same rules on 1-hour bars over ~720 sessions — a different timeframe, used only as a
        larger, independent robustness check of each idea (not as the strategy's own result)

Discipline (all set before looking at results):
  * Baseline V2 and every variant use the same engine, costs ("base" scenario) and no look-ahead.
  * Learned rules (time blocks, regime actions) are learned on TRAINING windows only.
  * Walk-forward: train → test windows roll forward; only test windows are reported as out-of-sample.
  * Selection uses metrics.objective (edge + recovery in R), never total P&L.
"""
import datetime as dt, math, os, sys, time, warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from . import costs, data, engine, metrics as M, news_risk

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "v3")
os.makedirs(OUT, exist_ok=True)
LINES = []


def out(s=""):
    print(s, flush=True)
    LINES.append(s)


def table(df, floatfmt=2):
    if df is None or len(df) == 0:
        out("_(none)_"); out(); return
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].map(lambda v: "—" if v != v else ("∞" if v == float("inf") else f"{v:,.{floatfmt}f}"))
    cols = [str(c) for c in d.columns]
    idx = d.index.name or ""
    out("| " + " | ".join([idx] + cols) + " |")
    out("|" + "---|" * (len(cols) + 1))
    for i, row in d.iterrows():
        out("| " + " | ".join([str(i)] + [str(v) for v in row.values]) + " |")
    out()


# ---------------------------------------------------------------- setup
COST = "base"
P5 = engine.prepare(data.load("5m"))
P60 = engine.prepare(data.load("60m"))
D5 = [d for d in P5["days"] if d >= dt.date(2026, 7, 23)]           # same 55-day window as the V2 run
D60 = [d for d in P60["days"] if d >= dt.date(2023, 12, 15)]        # after indicator warm-up

V2 = dict(COST=COST)                                                # all V3 switches off
SHORT = {"STRONG_BULL": "S.BULL", "STRONG_BEAR": "S.BEAR", "WEAK_BULL": "W.BULL", "WEAK_BEAR": "W.BEAR",
         "HIGH_VOLATILITY": "HI.VOL", "LOW_VOLATILITY": "LO.VOL"}


def ev(P, cfg, days):
    r = engine.run(P, dict(cfg, EVAL_FROM=days[0], EVAL_TO=days[-1]))
    t = r["trades"]
    if len(t):
        t = t[(t.date >= days[0]) & (t.date <= days[-1])]
    return t, r


def row(t, days):
    s = M.summary(t, days)
    return dict(trades=s["trades"], win=s.get("win_rate", 0), net=s["net"], avg=s.get("avg_trade", 0),
                pf=min(s.get("pf", 0), 99), expR=s.get("expectancy_R", 0), dd=s["max_dd"],
                worst_day=s.get("worst_day", 0), stop=s.get("stop_rate", 0))


def compare(variants, P, days, title):
    out(f"**{title}**"); out()
    rows = {}
    for name, cfg in variants:
        t, _ = ev(P, cfg, days)
        rows[name] = row(t, days)
    table(pd.DataFrame(rows).T.rename_axis("variant"), 2)
    return rows


# ---------------------------------------------------------------- walk-forward helpers
def folds(days, train, test, step=None):
    step = step or test
    res, i = [], 0
    while i + train + test <= len(days):
        res.append((days[i:i + train], days[i + train:i + train + test]))
        i += step
    return res


def learn_time_blocks(pool):
    blocks = []
    if not len(pool):
        return blocks
    pool = pool.assign(b=pool.signal_tod.map(M.bucket))
    for name, a, b in M.BUCKETS:
        x = pool[pool.b == name]
        if len(x) >= 6 and x.R.mean() < 0:
            blocks.append((a + 1, b + 1))
    return blocks


def learn_regimes(pool):
    acts = {}
    for reg, x in pool.groupby("regime") if len(pool) else []:
        if len(x) < 6:
            continue
        pf = x.net[x.net > 0].sum() / -x.net[x.net <= 0].sum() if (x.net <= 0).any() else 9
        if x.R.mean() < -0.15 and pf < 0.8:
            acts[reg] = "BLOCK"
        elif x.R.mean() < 0:
            acts[reg] = "RESTRICT"
    return acts


def learned(P, train, base_cfg):
    """Rules learned from the UNFILTERED candidate pool (relaxed signals, both indices) in the train window."""
    pool, _ = ev(P, dict(base_cfg, SIGNAL_MODE="relaxed", INDICES=("NIFTY", "SENSEX"),
                         MAX_TRADES_PER_DAY=99, MAX_CONSECUTIVE_LOSSES=99, MAX_DAILY_LOSS=1e9), train)
    return learn_time_blocks(pool), learn_regimes(pool)


def resolve(P, cfg, train):
    """Fill in learned rules for configs that ask for them."""
    cfg = dict(cfg)
    if cfg.pop("LEARN", False):
        tb, ra = learned(P, train, cfg)
        if cfg.get("ENABLE_TIME_FILTER"):
            cfg["TIME_BLOCKS"] = tb
        if cfg.get("ENABLE_REGIME_FILTER"):
            cfg["REGIME_ACTIONS"] = ra
    return cfg


def oos_consistency(P, days, fl, variants, title):
    """Each variant (fixed, or with rules learned on that fold's train window) vs V2 on every test window."""
    out(f"**{title}** — {len(fl)} folds; 'better' = higher objective than V2 on the unseen test window"); out()
    base_by_fold = []
    base_t = []
    for tr_days, te_days in fl:
        t, _ = ev(P, V2, te_days)
        base_by_fold.append(M.objective(t, min_trades=1)); base_t.append(t)
    rnd = lambda d: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}
    rows = {"V2 (baseline)": rnd(dict(row(pd.concat(base_t), [d for _, te in fl for d in te]), better="—"))}
    for name, cfg in variants:
        ts, wins = [], 0
        for k, (tr_days, te_days) in enumerate(fl):
            c = resolve(P, cfg, tr_days)
            t, _ = ev(P, c, te_days)
            ts.append(t)
            wins += M.objective(t, min_trades=1) > base_by_fold[k]
        allt = pd.concat(ts) if ts else pd.DataFrame()
        rows[name] = rnd(dict(row(allt, [d for _, te in fl for d in te]), better=f"{wins}/{len(fl)}"))
    table(pd.DataFrame(rows).T.rename_axis("variant (OOS pooled)"), 2)
    return rows


# ================================================================ 1. trade analysis
def s1():
    out("## 1. Every V2 trade (5-min, 55 days)"); out()
    t, r = ev(P5, dict(V2, COST="v2_model", IV_SAME_DAY=True), D5)
    out(f"Engine reproduces backtest.py exactly with V2's own cost model: {len(t)} trades, net ₹{t.net.sum():,.0f}. "
        "Everything below uses the stricter 'base' costs and no same-day VIX (no look-ahead in option pricing)."); out()
    t, r = ev(P5, V2, D5)
    t = t.assign(win=t.net > 0, bucket=t.signal_tod.map(M.bucket))
    t.to_csv(os.path.join(OUT, "v2_trades_diagnostics.csv"), index=False)
    s = M.summary(t, D5)
    out(f"V2 with base costs: {s['trades']} trades, win {s['win_rate']:.0f}%, net ₹{s['net']:,.0f}, PF {s['pf']:.2f}, "
        f"max DD ₹{s['max_dd']:,.0f}, worst day ₹{s['worst_day']:,.0f}"); out()
    cols = ["score", "adx", "rsi", "atr_rank", "vol_ratio", "vwap_dist", "ext21", "side_votes", "risk_pts", "itm_pts",
            "premium", "hold_min", "mfe_R", "mae_R", "c_trend", "c_momentum", "c_volume", "c_structure", "c_htf", "c_entry"]
    out("Median value at entry — winners vs losers:"); out()
    table(t.groupby("win")[cols].median().T.rename(columns={False: "losers", True: "winners"}).rename_axis("feature"))
    for k, lab in [("index", "Index"), ("side", "Direction"), ("bucket", "Entry time"), ("regime", "Regime"),
                   ("expiry_day", "Expiry day"), ("htf_dir", "Higher-TF direction"), ("mq_decision", "Market-quality decision"),
                   ("reason", "Exit reason")]:
        out(f"By {lab}:"); out()
        table(M.breakdown(t, k)[["trades", "win_rate", "net", "avg", "avg_win", "avg_loss", "exp_R", "pf"]].rename_axis(lab))
    t5 = t
    t60, _ = ev(P60, V2, D60)
    t60 = t60.assign(win=t60.net > 0, bucket=t60.signal_tod.map(M.bucket))
    out("Same breakdowns on the 1-hour robustness set (V2 rules, ~720 sessions):"); out()
    for k, lab in [("index", "Index"), ("side", "Direction"), ("bucket", "Entry time"), ("regime", "Regime")]:
        table(M.breakdown(t60, k)[["trades", "win_rate", "net", "exp_R", "pf"]].rename_axis(lab))
    return t5, t60


# ================================================================ 2. score thresholds
def s2():
    out("## 2. Trade-quality score thresholds"); out()
    out("Weights (fixed before testing): " + ", ".join(f"{k} {v}" for k, v in engine.S.WEIGHTS.items() if v)); out()
    var = [(f"score ≥ {s}", dict(V2, ENABLE_SCORE_FILTER=True, MIN_SCORE=s)) for s in (50, 55, 60, 65, 70, 75)]
    compare([("V2", V2)] + var, P5, D5, "5-min, full 55 days (in-sample — for reference only)")
    compare([("V2", V2)] + var, P60, D60, "1-hour, ~720 sessions")
    oos_consistency(P60, D60, folds(D60, 120, 60), var, "1-hour walk-forward")


# ================================================================ 3. entry filters
FILTERS = [
    ("trend agreement", dict(V2, ENABLE_TREND_FILTER=True)),
    ("sideways → reject", dict(V2, ENABLE_SIDEWAYS_FILTER=True, SIDEWAYS_MODE="reject")),
    ("sideways → need score 75", dict(V2, ENABLE_SIDEWAYS_FILTER=True, SIDEWAYS_MODE="raise", SIDEWAYS_MIN_SCORE=75)),
    ("no 15-min HTF filter (V2 had it)", dict(V2, HTF_FILTER=False)),
]


def s3():
    out("## 3. Entry filters"); out()
    compare([("V2", V2)] + FILTERS, P5, D5, "5-min, full period")
    compare([("V2", V2)] + FILTERS, P60, D60, "1-hour, ~720 sessions")
    oos_consistency(P60, D60, folds(D60, 120, 60), FILTERS, "1-hour walk-forward")


# ================================================================ 4. regimes
def s4(t5, t60):
    out("## 4. Market regimes"); out()
    pool60, _ = ev(P60, dict(V2, SIGNAL_MODE="relaxed", MAX_TRADES_PER_DAY=99, MAX_CONSECUTIVE_LOSSES=99,
                             MAX_DAILY_LOSS=1e9), D60)
    for lab, t in [("5-min V2 trades", t5), ("1-hour V2 trades", t60), ("1-hour unfiltered candidate pool", pool60)]:
        out(f"{lab}:"); out()
        b = M.breakdown(t, "regime")[["trades", "win_rate", "avg", "exp_R", "pf", "max_dd"]]
        acts = learn_regimes(t)
        b["action (rule)"] = [acts.get(i, "ALLOW") if n >= 6 else "ALLOW (n<6)" for i, n in zip(b.index, b.trades)]
        table(b.rename_axis("regime"))
    out("Rule (set in advance): n ≥ 6 and expectancy < −0.15R with PF < 0.8 → BLOCK; expectancy < 0 → RESTRICT "
        "(needs score + 10); otherwise ALLOW. In walk-forward the rule is learned per training window."); out()
    oos_consistency(P60, D60, folds(D60, 120, 60),
                    [("regime filter (learned)", dict(V2, ENABLE_REGIME_FILTER=True, ENABLE_SCORE_FILTER=False, LEARN=True))],
                    "1-hour walk-forward")


# ================================================================ 5. stops
STOPS = [("V2 swing 0.6–1.5 ATR", V2)] + \
    [(f"ATR × {k}", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="atr", SL_ATR_MULT=k)) for k in (0.75, 1.0, 1.25, 1.5)] + \
    [("recent swing, 0.5–2 ATR", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="recent")),
     ("market structure (confirmed swing)", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="structure")),
     ("0.20 % of index", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="pct", SL_PCT=0.2)),
     ("0.30 % of index", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="pct", SL_PCT=0.3))] + \
    [(f"hybrid max(struct, {k}×ATR)", dict(V2, ENABLE_DYNAMIC_SL=True, SL_MODE="hybrid", SL_ATR_MULT=k)) for k in (0.75, 1.0, 1.25)]


def s5():
    out("## 5. Stop-loss logic"); out()
    compare(STOPS, P5, D5, "5-min, full period")
    compare(STOPS, P60, D60, "1-hour, ~720 sessions")
    oos_consistency(P60, D60, folds(D60, 120, 60), STOPS[1:], "1-hour walk-forward")


# ================================================================ 6. targets
TARGETS = [(f"1:{rr:g}", dict(V2, RR=rr)) for rr in (1.5, 1.75, 2.25, 2.5, 3.0)] + [
    ("50% at 1R, rest at 2.5R", dict(V2, RR=2.5, ENABLE_DYNAMIC_TARGET=True, PARTIAL_AT_R=1.0, PARTIAL_FRAC=0.5)),
    ("cost at 1R, trail 1R from 1.5R, target 2R", dict(V2, RR=2.0, ENABLE_DYNAMIC_TARGET=True, TRAIL_START_R=1.5, TRAIL_R=1.0)),
    ("cost at 1R, trail 1R from 1.5R, target 2.5R", dict(V2, RR=2.5, ENABLE_DYNAMIC_TARGET=True, TRAIL_START_R=1.5, TRAIL_R=1.0)),
    ("no move to cost (BE off)", dict(V2, BE_AT_R=0)),
]


def s6():
    out("## 6. Target management (V2 = cost at 1R, target 1:2)"); out()
    compare([("V2 1:2", V2)] + TARGETS, P5, D5, "5-min, full period")
    compare([("V2 1:2", V2)] + TARGETS, P60, D60, "1-hour, ~720 sessions")
    oos_consistency(P60, D60, folds(D60, 120, 60), TARGETS, "1-hour walk-forward")


# ================================================================ 7. time of day
def s7(t5, t60):
    out("## 7. Time of day (entry = signal bar close)"); out()
    for lab, t in [("5-min V2", t5), ("1-hour V2", t60)]:
        out(f"{lab}:"); out()
        t = t.assign(bucket=t.signal_tod.map(M.bucket))
        table(M.breakdown(t, "bucket")[["trades", "win_rate", "avg", "avg_win", "avg_loss", "exp_R", "pf", "max_dd"]].rename_axis("time"))
    fixed = [("no entries before 10:00", dict(V2, ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, 601)])),
             ("no entries before 10:30", dict(V2, ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, 631)])),
             ("no entries before 11:00", dict(V2, ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, 661)]))]
    compare([("V2", V2)] + fixed, P5, D5, "5-min, fixed cut-offs (full period)")
    oos_consistency(P5, D5, folds(D5, 25, 10),
                    fixed + [("time filter learned per fold", dict(V2, ENABLE_TIME_FILTER=True, LEARN=True))],
                    "5-min walk-forward (train 25 days → test 10)")
    oos_consistency(P60, D60, folds(D60, 120, 60),
                    [("first bar blocked (≈ before 10:15)", dict(V2, ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, 616)])),
                     ("time filter learned per fold", dict(V2, ENABLE_TIME_FILTER=True, LEARN=True))],
                    "1-hour walk-forward")


# ================================================================ 8. Nifty vs Sensex
IDX = [("NIFTY only", dict(V2, INDICES=("NIFTY",))), ("SENSEX only", dict(V2, INDICES=("SENSEX",))),
       ("both, Nifty preferred (V2)", V2),
       ("both, Sensex only if score ≥ 70", dict(V2, ENABLE_SENSEX_FILTER=True, SENSEX_MIN_SCORE=70)),
       ("both, Sensex only if score ≥ 75", dict(V2, ENABLE_SENSEX_FILTER=True, SENSEX_MIN_SCORE=75))]


def s8():
    out("## 8. NIFTY vs SENSEX"); out()
    compare(IDX, P5, D5, "5-min, full period")
    compare(IDX, P60, D60, "1-hour, ~720 sessions")
    for lab, P, days in [("5-min", P5, D5), ("1-hour", P60, D60)]:
        a, _ = ev(P, dict(V2, INDICES=("NIFTY",)), days)
        b, _ = ev(P, dict(V2, INDICES=("SENSEX",)), days)
        da, db = M.daily(a, days), M.daily(b, days)
        both = (da != 0) & (db != 0)
        corr = np.corrcoef(da[both], db[both])[0, 1] if both.sum() > 3 else float("nan")
        out(f"{lab}: correlation of daily P&L on days both traded = {corr:.2f} ({int(both.sum())} days); "
            f"Sensex-only expectancy {b.R.mean():+.2f}R vs Nifty-only {a.R.mean():+.2f}R")
    out()
    oos_consistency(P60, D60, folds(D60, 120, 60), IDX[:2] + IDX[3:], "1-hour walk-forward")


# ================================================================ 9. market quality
def s9():
    out("## 9. Market-quality filter (upgraded crude agent)"); out()
    mq = [("crude veto off", dict(V2, USE_CRUDE_VETO=False))] + [
        (f"MQ allow≥{a}, reduce≥{r}, wait≥{w}", dict(V2, ENABLE_MARKET_QUALITY=True, MQ_THRESHOLDS=dict(allow=a, reduce=r, wait=w)))
        for a, r, w in ((75, 60, 40), (70, 55, 35), (65, 50, 30), (60, 45, 25))]
    compare([("V2 (binary crude veto)", V2)] + mq, P5, D5, "5-min, full period")
    compare([("V2 (binary crude veto)", V2)] + mq, P60, D60, "1-hour, ~720 sessions (Brent history from May 2024)")
    oos_consistency(P60, D60, folds(D60, 120, 60), mq, "1-hour walk-forward")


# ================================================================ 10–11. sizing and loss streaks
def s10():
    out("## 10. Position sizing and daily risk limits (4 lots base)"); out()
    sz = [("score sizing: ≥75 → 4 lots, else 2", dict(V2, ENABLE_DYNAMIC_SIZING=True, SIZE_HIGH_SCORE=75, LOTS_MED=2)),
          ("risk budget ₹6,000 per trade", dict(V2, ENABLE_RISK_BUDGET=True, MAX_OPEN_RISK=6000)),
          ("risk budget ₹8,000 per trade", dict(V2, ENABLE_RISK_BUDGET=True, MAX_OPEN_RISK=8000)),
          ("daily loss ₹8,000", dict(V2, MAX_DAILY_LOSS=8000)),
          ("daily loss ₹8,000 + budget ₹6,000", dict(V2, MAX_DAILY_LOSS=8000, ENABLE_RISK_BUDGET=True, MAX_OPEN_RISK=6000)),
          ("max 2 trades/day", dict(V2, MAX_TRADES_PER_DAY=2))]
    compare([("V2", V2)] + sz, P5, D5, "5-min, full period")
    compare([("V2", V2)] + sz, P60, D60, "1-hour, ~720 sessions")
    oos_consistency(P60, D60, folds(D60, 120, 60), sz, "1-hour walk-forward")
    out("## 11. Consecutive-loss protection"); out()
    cl = [("no streak lock", dict(V2, MAX_CONSECUTIVE_LOSSES=99)),
          ("stop after 3", dict(V2, MAX_CONSECUTIVE_LOSSES=3)),
          ("half size after 2, stop after 3", dict(V2, MAX_CONSECUTIVE_LOSSES=3, ENABLE_DYNAMIC_SIZING=True, SIZE_HIGH_SCORE=0,
                                                   LOSS_STREAK_SIZING={2: 0.5}))]
    compare([("V2: stop after 2 losses/day", V2)] + cl, P5, D5, "5-min, full period")
    compare([("V2: stop after 2 losses/day", V2)] + cl, P60, D60, "1-hour, ~720 sessions")
    t, _ = ev(P60, dict(V2, MAX_CONSECUTIVE_LOSSES=99, MAX_TRADES_PER_DAY=99), D60)
    t = t.sort_values(["date", "entry_time"]).reset_index(drop=True)
    prev_loss = (t.net.shift(1) < 0) & (t.date.shift(1) == t.date)
    out(f"1-hour, unrestricted: trade after a same-day loss — n={int(prev_loss.sum())}, expectancy "
        f"{t[prev_loss].R.mean():+.2f}R vs after a win/first trade {t[~prev_loss].R.mean():+.2f}R")
    out()


# ================================================================ 12. news
def s12():
    out("## 12. News / event-risk layer (5-min period only — headlines fetched per day)"); out()
    hist = news_risk.levels(D5, lookback=1)
    levels = {d: v["level"] for d, v in hist.items()}
    out("HIGH days: " + "; ".join(f"{d} ({', '.join(v['reasons'])})" for d, v in hist.items() if v["level"] == "HIGH")); out()
    cnt = pd.Series(levels).value_counts()
    out("Days by level (from the previous day's headlines only): " + ", ".join(f"{k} {v}" for k, v in cnt.items())); out()
    t, _ = ev(P5, V2, D5)
    t = t.assign(news=t.date.map(levels))
    table(M.breakdown(t, "news")[["trades", "win_rate", "net", "exp_R", "pf"]].rename_axis("news risk"))
    compare([("V2", V2),
             ("HIGH blocks", dict(V2, ENABLE_NEWS_FILTER=True, NEWS={d: ("HIGH" if v == "HIGH" else "LOW") for d, v in levels.items()})),
             ("HIGH blocks, MEDIUM half size", dict(V2, ENABLE_NEWS_FILTER=True, NEWS=levels, NEWS_MEDIUM_SCORE_ADD=0))],
            P5, D5, "5-min, full period")
    return levels


# ================================================================ 13. walk-forward selection
GRID = [dict(V2, **({} if tb is None else dict(ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, tb)])),
             **({} if bud is None else dict(ENABLE_RISK_BUDGET=True, MAX_OPEN_RISK=bud)),
             _label=f"time<{'none' if tb is None else f'{(tb - 1) // 60:02d}:{(tb - 1) % 60:02d}'} budget {bud or 'off'}")
        for tb in (None, 586, 601, 631, 661) for bud in (None, 5000, 6000, 8000)]


def pick(P, train):
    best, bj = None, -1e9
    for g in GRID:
        cfg = {k: v for k, v in g.items() if k != "_label"}
        t, _ = ev(P, cfg, train)
        j = M.objective(t)
        if j > bj:
            best, bj = g, j
    return best


def s13():
    out("## 13. Walk-forward validation (parameters chosen on training data only)"); out()
    out("Candidates (fixed in advance): entry cut-off ∈ {none, 09:45, 10:00, 10:30, 11:00} × per-trade risk budget "
        "∈ {off, ₹5k, ₹6k, ₹8k} = 20 configurations. Each training window picks one by `metrics.objective` "
        "(edge + recovery in R — not P&L); it is then run untouched on the next window."); out()
    res = {}
    for lab, train_n, test_n in (("rolling: train 22 days → test 11, 3 folds", 22, 11),
                                 ("anchored: train from day 1 → test 11, 3 folds", None, 11)):
        out(f"**{lab}**"); out()
        rows, ts, ts2 = [], [], []
        starts = [22, 33, 44]
        for st in starts:
            train = D5[st - 22:st] if train_n else D5[:st]
            test = D5[st:st + test_n]
            g = pick(P5, train)
            cfg = {k: v for k, v in g.items() if k != "_label"}
            t, _ = ev(P5, cfg, test); b, _ = ev(P5, V2, test)
            ts.append(t); ts2.append(b)
            rows.append(dict(test=f"{test[0]} → {test[-1]}", chosen=g["_label"], v3_trades=len(t),
                             v3_net=int(t.net.sum()) if len(t) else 0, v2_net=int(b.net.sum()) if len(b) else 0,
                             v3_dd=int(M.max_dd(M.daily(t, test).values)), v2_dd=int(M.max_dd(M.daily(b, test).values))))
        table(pd.DataFrame(rows).set_index("test"))
        alld = [d for st in starts for d in D5[st:st + test_n]]
        a, b = pd.concat(ts), pd.concat(ts2)
        table(pd.DataFrame({"V2 (OOS)": row(b, alld), "V3 walk-forward (OOS)": row(a, alld)}).T.rename_axis("pooled test windows"))
        res[lab] = (a, b, alld)
    out("**Static split: train (first 50%) → validate (next 25%) → test (last 25%)**"); out()
    n = len(D5); tr, va, te = D5[:n // 2], D5[n // 2:3 * n // 4], D5[3 * n // 4:]
    g = pick(P5, tr)
    cfg = {k: v for k, v in g.items() if k != "_label"}
    tv, _ = ev(P5, cfg, va); bv, _ = ev(P5, V2, va)
    passed = M.objective(tv, 1) >= M.objective(bv, 1)
    out(f"Train {tr[0]}→{tr[-1]} chose **{g['_label']}**. Validation {va[0]}→{va[-1]}: "
        f"objective {M.objective(tv, 1):.2f} vs V2 {M.objective(bv, 1):.2f} → {'kept' if passed else 'rejected, fall back to V2'}."); out()
    use = cfg if passed else V2
    tt, _ = ev(P5, use, te); bt, _ = ev(P5, V2, te)
    table(pd.DataFrame({"V2": row(bt, te), "selected": row(tt, te)}).T.rename_axis(f"test {te[0]}→{te[-1]}"))
    return res


# ================================================================ 14–16. assemble V3, walk-forward, Monte Carlo, costs
def assemble(decisions):
    v3 = dict(V2)
    for k, v in decisions.items():
        v3.update(v)
    return v3


def final(v3, v3_extra, news_levels, wf=None):
    out("## 13–16. Final comparison"); out()
    versions = [("Strategy V2", V2),
                ("V2 + improved filter", v3_extra["filter"]),
                ("Strategy V3", v3),
                ("V3 + dynamic SL", dict(v3, ENABLE_DYNAMIC_SL=True, SL_MODE="hybrid", SL_ATR_MULT=1.0)),
                ("V3 + dynamic sizing", dict(v3, ENABLE_DYNAMIC_SIZING=True, SIZE_HIGH_SCORE=75, LOTS_MED=2)),
                ("V3 + news filter", dict(v3, ENABLE_NEWS_FILTER=True, NEWS=news_levels, NEWS_MEDIUM_SCORE_ADD=0))]
    keys = [("trades", "Trades", "{:.0f}"), ("win_rate", "Win rate %", "{:.1f}"), ("loss_rate", "Loss rate %", "{:.1f}"),
            ("gross", "Gross P&L ₹", "{:,.0f}"), ("net", "Net P&L ₹", "{:,.0f}"), ("avg_trade", "Avg trade ₹", "{:,.0f}"),
            ("avg_win", "Avg win ₹", "{:,.0f}"), ("avg_loss", "Avg loss ₹", "{:,.0f}"), ("pf", "Profit factor", "{:.2f}"),
            ("expectancy_R", "Expectancy (R)", "{:+.2f}"), ("reward_risk", "Reward/risk", "{:.2f}"),
            ("max_dd", "Max drawdown ₹", "{:,.0f}"), ("max_dd_pct", "Max DD % (₹3L)", "{:.1f}"),
            ("recovery", "Recovery factor", "{:.2f}"), ("sharpe", "Sharpe", "{:.2f}"), ("sortino", "Sortino", "{:.2f}"),
            ("calmar", "Calmar", "{:.2f}"), ("win_days", "Winning days", "{:.0f}"), ("loss_days", "Losing days", "{:.0f}"),
            ("win_streak", "Longest win streak", "{:.0f}"), ("loss_streak", "Longest loss streak", "{:.0f}"),
            ("best_day", "Largest winning day ₹", "{:,.0f}"), ("worst_day", "Largest losing day ₹", "{:,.0f}"),
            ("stop_rate", "Stop-loss exits %", "{:.0f}")]
    for lab, P, days, vers in [("5-min, 55 days — in-sample (V3's rules were chosen using this period; see §13 for out-of-sample)", P5, D5, versions),
                               ("1-hour, ~720 sessions", P60, D60, [v for v in versions if "news" not in v[0]])]:
        out(f"**{lab}**"); out()
        cols = {}
        for name, cfg in vers:
            t, _ = ev(P, cfg, days)
            s = M.summary(t, days)
            cols[name] = {lab_: (fmt.format(s[k]) if s.get(k) not in (None, float("inf")) else "∞") for k, lab_, fmt in keys}
        table(pd.DataFrame(cols).rename_axis("metric"))

    for lab, P, days, fl in [("1-hour", P60, D60, folds(D60, 120, 60))]:
        out(f"**Walk-forward out-of-sample, {lab}** (rules learned on each train window, reported on the next unseen window)"); out()
        rows = {}
        for name, cfg in [("Strategy V2", V2), ("Strategy V3", v3)]:
            ts, per = [], []
            for tr_days, te_days in fl:
                t, _ = ev(P, resolve(P, cfg, tr_days), te_days)
                ts.append(t); per.append(t.net.sum() if len(t) else 0)
            allt = pd.concat(ts); alld = [d for _, te in fl for d in te]
            s = M.summary(allt, alld)
            rows[name] = dict(trades=s["trades"], win=s.get("win_rate", 0), net=s["net"], pf=min(s.get("pf", 0), 99),
                              expR=s.get("expectancy_R", 0), dd=s["max_dd"], worst_day=s.get("worst_day", 0),
                              positive_folds=f"{sum(p > 0 for p in per)}/{len(per)}")
            if name == "Strategy V3":
                v3_oos = (allt, alld)
            else:
                v2_oos = (allt, alld)
        table(pd.DataFrame(rows).T.rename_axis("version"))
        out(f"**Monte Carlo, {lab} walk-forward OOS trades** (10,000 shuffles of trade order; bootstrap of daily P&L)"); out()
        mc = {name: M.monte_carlo(t, d) for name, (t, d) in [("Strategy V2", v2_oos), ("Strategy V3", v3_oos)]}
        lbl = dict(dd_actual="Actual max DD ₹", dd_median="Expected (median) max DD ₹", dd_p95="95th-pct worst DD ₹",
                   dd_worst="Worst simulated DD ₹", month_median="Median 21-day month ₹", month_p5="5th-pct month ₹",
                   month_p95="95th-pct month ₹", p_neg_month="P(negative month) %", period_median="Median period ₹",
                   period_p5="5th-pct period ₹", p_loss_period="P(losing period) %")
        table(pd.DataFrame({k: {lbl[a]: (f"{b:,.0f}" if "₹" in lbl[a] else f"{b:.1f}") for a, b in v.items()} for k, v in mc.items() if v})
              .rename_axis("metric"))

    if wf:
        a, b, alld = wf["rolling: train 22 days → test 11, 3 folds"]
        out("**Monte Carlo, 5-min walk-forward OOS trades** (rolling folds; 10,000 shuffles / bootstraps)"); out()
        lbl = dict(dd_actual="Actual max DD ₹", dd_median="Expected (median) max DD ₹", dd_p95="95th-pct worst DD ₹",
                   dd_worst="Worst simulated DD ₹", month_median="Median 21-day month ₹", month_p5="5th-pct month ₹",
                   month_p95="95th-pct month ₹", p_neg_month="P(negative month) %", period_median="Median period ₹",
                   period_p5="5th-pct period ₹", p_loss_period="P(losing period) %")
        mc = {"Strategy V2": M.monte_carlo(b, alld), "Strategy V3 (WF)": M.monte_carlo(a, alld)}
        table(pd.DataFrame({k: {lbl[x]: (f"{y:,.0f}" if "₹" in lbl[x] else f"{y:.1f}") for x, y in v.items()} for k, v in mc.items() if v})
              .rename_axis("metric"))
        t3, _ = ev(P5, v3, D5); t2, _ = ev(P5, V2, D5)
        mc = {"Strategy V2": M.monte_carlo(t2, D5), "Strategy V3": M.monte_carlo(t3, D5)}
        out("**Monte Carlo, 5-min full period** (in-sample — the V3 rules were chosen using this period)"); out()
        table(pd.DataFrame({k: {lbl[x]: (f"{y:,.0f}" if "₹" in lbl[x] else f"{y:.1f}") for x, y in v.items()} for k, v in mc.items() if v})
              .rename_axis("metric"))
    out("**Cost scenarios (5-min, full period, V3)**"); out()
    rows = {}
    for sc in ("v2_model", "optimistic", "base", "conservative", "stress"):
        for name, cfg in [("V2", V2), ("V3", v3)]:
            t, _ = ev(P5, dict(cfg, COST=sc), D5)
            rows[f"{name} · {sc}"] = row(t, D5)
    table(pd.DataFrame(rows).T.rename_axis("version · costs"))

    t, r = ev(P5, v3, D5)
    t.to_csv(os.path.join(OUT, "v3_executed_trades.csv"), index=False)
    rj = r["rejects"]
    if len(rj):
        rj = rj[[c for c in ["ts", "index", "side", "score", "regime", "reason", "c_trend", "c_volume", "c_volatility",
                              "news", "mq", "mq_decision", "adx", "side_votes"] if c in rj.columns]]
        rj.to_csv(os.path.join(OUT, "v3_rejected_signals.csv"), index=False)
    out(f"Diagnostics: {len(t)} executed trades → v3_executed_trades.csv; {len(rj)} rejected signals → v3_rejected_signals.csv"); out()
    t = t.assign(bucket=t.signal_tod.map(M.bucket), month=t.date.map(lambda d: d.strftime("%Y-%m")))
    for k, lab in [("month", "Monthly P&L"), ("index", "Index-wise"), ("bucket", "Time-of-day"), ("regime", "Regime-wise")]:
        out(f"V3 {lab} (5-min):"); out()
        table(M.breakdown(t, k)[["trades", "win_rate", "net", "exp_R", "pf"]].rename_axis(lab))


if __name__ == "__main__":
    t0 = time.time()
    out("# Strategy V3 research — results"); out()
    out(f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}. Costs: '{COST}' scenario unless stated. Lots: 4. "
        "Option prices are modelled (Black-Scholes on India VIX), not real fills."); out()
    t5, t60 = s1()
    s2(); s3(); s4(t5, t60); s5(); s6(); s7(t5, t60); s8(); s9(); s10()
    news_levels = s12()
    wf = s13()
    # ---- V3 = only the changes that passed (filled in from the evidence; see V3_REPORT.md)
    from .v3_config import V3, V3_FILTER_ONLY
    final(dict(V2, **V3), dict(filter=dict(V2, **V3_FILTER_ONLY)), news_levels, wf)
    with open(os.path.join(OUT, "results.md"), "w") as f:
        f.write("\n".join(LINES))
    print(f"\nDone in {time.time() - t0:.0f}s → reports/v3/results.md")
