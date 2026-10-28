"""
Controlled comparison of scalper variants on ONE frozen dataset, one engine, one cost model.

    python -m scalp_lab.experiments            # → reports/scalp_lab/ (RESULTS.md, results.json, CSVs)

Every experiment runs under base and conservative fills (lab engine, see engine.py) and charges-only for
reference. Selection never uses total P&L: the objective is expectancy in R averaged over base and
conservative fills, with a minimum trade count. Walk-forward is chronological (anchored): choose on the
earlier days only, score on the next unseen days.
"""
import json
import os
import random
import time
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from . import data as D
from . import diagnose, engine
from .exits import EXIT_DEFAULTS
from .regime import REGIME_DEFAULTS
from .strategies import BRT_DEFAULTS, SWEEP_DEFAULTS

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "scalp_lab")
FILLS = ("base", "conservative")
REG = {}   # regime filter with its default thresholds

EXPERIMENTS = {
    "A baseline (volume rule, 5/10)": dict(strategy="volume"),
    "B sweep reversal": dict(strategy="sweep"),
    "B2 sweep reversal + structure stop": dict(strategy="sweep", exit=dict(EXIT_POLICY="structure")),
    "C breakout-retest": dict(strategy="retest"),
    "C2 breakout, no retest": dict(strategy="retest", strat_cfg=dict(BRT_RETEST=False)),
    "C3 breakout-retest + structure stop": dict(strategy="retest", exit=dict(EXIT_POLICY="structure")),
    "D baseline + regime filter": dict(strategy="volume", regime=REG),
    "E0 baseline, no volume requirement": dict(strategy="volume", strat_cfg=dict(SCALP_VOL_MULT=0)),
    "E1 volume 2.0x": dict(strategy="volume", strat_cfg=dict(SCALP_VOL_MULT=2.0)),
    "E2 volume 1.5x + displacement": dict(strategy="volume", strat_cfg=dict(VOL_DISP_ATR=0.6)),
    "E3 volume 1.5x + retest confirm": dict(strategy="retest", strat_cfg=dict(BRT_RANGE_N=5, BRT_RVOL_MIN=1.5)),
    "F1 baseline + volatility stop": dict(strategy="volume", exit=dict(EXIT_POLICY="vol")),
    "F2 baseline + structure stop": dict(strategy="volume", exit=dict(EXIT_POLICY="structure")),
    "F3 baseline + ATR trail": dict(strategy="volume", exit=dict(TRAIL="atr")),
    "F4 baseline + profit lock": dict(strategy="volume", exit=dict(TRAIL="lock")),
    "F5 baseline + target extension": dict(strategy="volume", exit=dict(EXTEND=True)),
    "F6 baseline, no break-even": dict(strategy="volume", exit=dict(BE_AT_POINTS=0)),
}
# the scalper's CURRENT settings (from 2026-10-15): 15-pt stop, 30-pt target, cost at +15, ₹2,500/day, 30-min time stop
NEW_EXIT = dict(SL_POINTS=15.0, TG_POINTS=30.0, BE_AT_POINTS=15.0)
NEW_RISK = dict(SCALP_MAX_DAILY_LOSS=2500, SCALP_MAX_HOLD_MIN=30)
EXPERIMENTS.update({
    "H1 volume rule · 15/30": dict(strategy="volume", exit=NEW_EXIT, risk=NEW_RISK),
    "H2 sweep reversal · 15/30": dict(strategy="sweep", exit=NEW_EXIT, risk=NEW_RISK),
    "H3 breakout-retest · 15/30": dict(strategy="retest", exit=NEW_EXIT, risk=NEW_RISK),
    "H4 breakout, no retest · 15/30": dict(strategy="retest", strat_cfg=dict(BRT_RETEST=False), exit=NEW_EXIT, risk=NEW_RISK),
    "H5 volume rule + regime filter · 15/30": dict(strategy="volume", regime=REG, exit=NEW_EXIT, risk=NEW_RISK),
})


def v2_trades(data, fills):
    """Scalper V2 (the active scalper rule) at the current 15/30 settings, through its own backtest engine."""
    import scalp_v2_backtest as B
    M2 = B.Market(data, ["NIFTY"])
    cfg = B.base_cfg(B.V2, SCALP_DYNAMIC_SL=False, SCALP_SL_POINTS=15, SCALP_RR=2.0, SCALP_BE_AT_R=1.0,
                     SCALP_MAX_DAILY_LOSS=2500, SCALP_MAX_HOLD_MIN=30)
    t = B.simulate(M2, cfg, fills)
    if len(t):
        t = t.assign(slippage=0.0)        # V2's engine prices slippage into the fills; not reported separately
    return t

# small, pre-declared candidate sets for the walk-forward (chosen on train days only)
FAMILIES = {
    "baseline": [dict(strategy="volume")],
    "sweep": [dict(strategy="sweep", strat_cfg=dict(SWEEP_RECLAIM_BARS=r, SWEEP_DISP_ATR=d))
              for r in (2, 3) for d in (0.3, 0.5)],
    "retest": [dict(strategy="retest", strat_cfg=dict(BRT_RANGE_N=n, BRT_RETEST_BARS=b)) for n in (5, 10) for b in (4, 6)],
    "baseline+regime": [dict(strategy="volume", regime=dict(REG_MAX_VWAP_CROSSES=x, REG_FLAT_VWAP_PCT=f))
                        for x in (3, 5) for f in (0.02, 0.03)],
    "volume variants": [dict(strategy="volume", strat_cfg=dict(SCALP_VOL_MULT=m, VOL_DISP_ATR=dd))
                        for m in (1.5, 2.0) for dd in (0, 0.6)],
}

SENSITIVITY = {
    "sweep": ("sweep", [("SWEEP_MIN_PEN_ATR", (0.05, 0.10, 0.20)), ("SWEEP_RECLAIM_BARS", (1, 2, 3, 5)),
                        ("SWEEP_DISP_ATR", (0.3, 0.5, 0.7)), ("SWEEP_MIN_REWARD_R", (1.0, 1.5, 2.0)),
                        ("SWEEP_HTF", ("off", "not_against", "with"))]),
    "retest": ("retest", [("BRT_RANGE_N", (5, 10, 15)), ("BRT_RETEST_BARS", (3, 6, 9)),
                          ("BRT_RETEST_TOL_ATR", (0.15, 0.25, 0.4)), ("BRT_BODY_MIN", (0.4, 0.5, 0.6)),
                          ("BRT_HTF", ("off", "not_against", "with"))]),
    "regime": ("volume", [("REG_MAX_VWAP_CROSSES", (3, 4, 5, 6)), ("REG_FLAT_VWAP_PCT", (0.01, 0.02, 0.03)),
                          ("REG_LOW_EXPANSION", (0.5, 0.6, 0.7))]),
}


# ---------------------------------------------------------------------------------------------- metrics
def metrics(t):
    if t is None or not len(t):
        return dict(trades=0, wins=0, losses=0, win_rate=0.0, gross=0, charges=0, slippage=0, net=0, pf=0.0,
                    exp_rs=0, exp_R=0.0, avg_win=0, avg_loss=0, rr=0.0, max_dd=0, max_consec_losses=0, avg_hold=0.0,
                    ci_low=0, ci_high=0, p_positive=0.0)
    w, l = t[t.net > 0], t[t.net <= 0]
    eq = t.net.cumsum()
    streak = worst = 0
    for x in t.net:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    lo, hi, pp = bootstrap(t.net.values)
    aw, al = (w.net.mean() if len(w) else 0), (l.net.mean() if len(l) else 0)
    return dict(trades=len(t), wins=len(w), losses=len(l), win_rate=round(100 * len(w) / len(t), 1),
                gross=int(t.gross.sum()), charges=int(t.charges.sum()), slippage=int(t.slippage.sum()),
                net=int(t.net.sum()), pf=round(w.net.sum() / -l.net.sum(), 2) if l.net.sum() < 0 else 99.0,
                exp_rs=int(t.net.mean()), exp_R=round(t.R.mean(), 3), avg_win=int(aw), avg_loss=int(al),
                rr=round(aw / -al, 2) if al < 0 else 0.0, max_dd=int((eq - eq.cummax().clip(lower=0)).min()),
                max_consec_losses=worst, avg_hold=round(t.hold_min.mean(), 1), ci_low=int(lo), ci_high=int(hi),
                p_positive=round(pp, 2))


def bootstrap(x, n=2000, seed=7):
    """95% CI of the mean trade and the share of resampled trade sets with a positive total (Monte Carlo)."""
    if len(x) < 2:
        return (x.mean() if len(x) else 0), (x.mean() if len(x) else 0), float(len(x) and x.sum() > 0)
    rnd = np.random.default_rng(seed)
    s = rnd.choice(x, size=(n, len(x)), replace=True)
    means = s.mean(axis=1)
    return np.percentile(means, 2.5), np.percentile(means, 97.5), float((s.sum(axis=1) > 0).mean())


def objective(ts, min_trades):
    """Mean expectancy (R) over fill models; too few trades = unusable. Never total P&L."""
    vals = []
    for t in ts:
        if len(t) < min_trades:
            return -9.0
        vals.append(t.R.mean())
    return float(np.mean(vals))


def by(t, key):
    if not len(t):
        return {}
    return {str(k): {x: metrics(g)[x] for x in ("trades", "win_rate", "net", "exp_R", "pf")} for k, g in t.groupby(key)}


# ---------------------------------------------------------------------------------------------- runner
class Runner:
    """Runs each (config, fills) once over all days; walk-forward folds slice the trades by day (entries on a
    day don't depend on other days being tradable: daily limits reset, indicators run continuously)."""

    def __init__(self, M):
        self.M, self.cache = M, {}

    def run(self, exp, fills):
        key = (json.dumps(exp, sort_keys=True, default=str), fills)
        if key not in self.cache:
            self.cache[key] = engine.run(self.M, exp, fills=fills, collect_setups=True)
        return self.cache[key]

    def trades(self, exp, fills, days=None):
        t = self.run(exp, fills)["trades"]
        return t if days is None or not len(t) else t[t.date.isin(set(days))]


def walk_forward(R, train=7, test=3, step=3):
    days = R.M.trade_days
    folds = []
    i = train
    while i + test <= len(days):
        folds.append((days[:i], days[i:i + test]))
        i += step
    out, rows = {}, []
    for fam, cands in FAMILIES.items():
        oos = {f: [] for f in FILLS}
        for k, (tr, te) in enumerate(folds, 1):
            scored = [(objective([R.trades(c, f, tr) for f in FILLS], min_trades=5), j) for j, c in enumerate(cands)]
            best_score, j = max(scored)
            chosen = cands[j]
            for f in FILLS:
                t = R.trades(chosen, f, te)
                oos[f].append(t)
                m = metrics(t)
                rows.append(dict(family=fam, fold=k, train=f"{tr[0]}→{tr[-1]}", test=f"{te[0]}→{te[-1]}", fills=f,
                                 chosen=json.dumps({**chosen.get("strat_cfg", {}), **(chosen.get("regime") or {})}),
                                 train_obj=round(best_score, 3), trades=m["trades"], net=m["net"], exp_R=m["exp_R"]))
        out[fam] = {f: metrics(pd.concat([x for x in oos[f] if len(x)]) if any(len(x) for x in oos[f]) else None)
                    for f in FILLS}
    return folds, out, rows


def sensitivity(R):
    rows = []
    for fam, (strat, params) in SENSITIVITY.items():
        for name, vals in params:
            for v in vals:
                if fam == "regime":
                    exp = dict(strategy=strat, regime={name: v})
                else:
                    exp = dict(strategy=strat, strat_cfg={name: v})
                m = {f: metrics(R.trades(exp, f)) for f in FILLS}
                default = (REGIME_DEFAULTS if fam == "regime" else SWEEP_DEFAULTS if fam == "sweep" else BRT_DEFAULTS)[name]
                rows.append(dict(family=fam, param=name, value=str(v), default=v == default,
                                 trades=m["base"]["trades"], exp_R_base=m["base"]["exp_R"], net_base=m["base"]["net"],
                                 exp_R_cons=m["conservative"]["exp_R"], net_cons=m["conservative"]["net"]))
    return rows


def volume_value(M):
    """Does NIFTYBEES relative volume say anything about NIFTY's next 5 minutes? Quintiles of rvol vs the
    move that follows (absolute size, and continuation in the direction of the candle's body)."""
    from .features import Features
    F, rows = Features(), []
    bars = M.bars
    for i, b in enumerate(bars):
        F.update(b)
        if F.rvol is None or i + 5 >= len(bars) or bars[i + 5]["t"].date() != b["t"].date():
            continue
        d = 1 if b["c"] >= b["o"] else -1
        fwd = bars[i + 5]["c"] - b["c"]
        rows.append(dict(rvol=F.rvol, abs_fwd=abs(fwd), cont=fwd * d, atr=F.atr))
    df = pd.DataFrame(rows)
    df["q"] = pd.qcut(df.rvol, 5, labels=["Q1 lowest", "Q2", "Q3", "Q4", "Q5 highest"])
    g = df.groupby("q").agg(rvol_mid=("rvol", "median"), n=("rvol", "size"), abs_move_pts=("abs_fwd", "mean"),
                            continuation_pts=("cont", "mean"), atr=("atr", "mean")).round(2)
    spike = df[df.rvol >= 1.5]
    corr = float(np.corrcoef(df.rvol, df.abs_fwd)[0, 1])
    corr_dir = float(np.corrcoef(df.rvol, df.cont)[0, 1])
    return g.reset_index().to_dict("records"), dict(n=len(df), corr_abs=round(corr, 3), corr_cont=round(corr_dir, 3),
                                                    spike_share=round(len(spike) / len(df), 3),
                                                    spike_cont=round(spike.cont.mean(), 2), all_cont=round(df.cont.mean(), 2))


# ---------------------------------------------------------------------------------------------- main
def main():
    t0 = time.time()
    data, meta = D.load()
    M = engine.Market(data)
    R = Runner(M)
    os.makedirs(OUT, exist_ok=True)
    res = dict(generated=datetime.now().strftime("%Y-%m-%d %H:%M"), dataset=meta,
               period=f"{M.trade_days[0]} → {M.trade_days[-1]}", days=len(M.trade_days), instrument="NIFTY",
               lots=1, risk=engine.RISK, experiments={}, notes=[])

    # 1. baseline reproduction (old engine == lab engine in compat mode)
    import scalp_backtest
    repro = {}
    for f in FILLS:
        with engine.legacy_config():
            old = scalp_backtest.run(data, ["NIFTY"], 1, f, strat="volume")
        new = engine.run(M, dict(strategy="volume"), fills=f, compat=True)["trades"]
        same = len(old) == len(new) and (old[["entry_time", "exit_time", "side"]].values ==
                                          new[["entry_time", "exit_time", "side"]].values).all()
        repro[f] = dict(old_trades=len(old), old_net=int(old.net.sum()), new_trades=len(new),
                        new_net=int(new.net.sum()), identical_trades=bool(same))
    res["reproduction"] = repro

    # 2. experiments
    for name, exp in EXPERIMENTS.items():
        e = {}
        for f in FILLS + ("v2_model",):
            r = R.run(exp, f)
            t = r["trades"]
            e[f] = metrics(t)
            if f == "base":
                e["rejected_signals"] = len(r["rejections"])
                e["rejections_by_reason"] = pd.Series([x["reason"].split(":")[0] for x in r["rejections"]]
                                                      ).value_counts().head(8).to_dict() if r["rejections"] else {}
                e["setups"] = pd.Series([s["status"] for s in r["setups"]]).value_counts().to_dict() if r["setups"] else {}
                e["by_session"] = by(t, "tod")
                e["by_regime"] = by(t, "regime")
                e["by_side"] = by(t, "side")
                if len(t):
                    t.drop(columns=["events"]).to_csv(os.path.join(OUT, "trades_" + name.split()[0] + ".csv"), index=False)
                    rows, costs = diagnose.summary(t, M)
                    e["loss_causes"], e["costs"] = rows, costs
                    ev = [dict(trade=i, **x) for i, evs in enumerate(t.events) for x in evs]
                    e["adjustments"] = len(ev)
                    if ev:
                        pd.DataFrame(ev).to_csv(os.path.join(OUT, "adjustments_" + name.split()[0] + ".csv"), index=False)
                if r["setups"]:
                    pd.DataFrame(r["setups"]).to_csv(os.path.join(OUT, "setups_" + name.split()[0] + ".csv"), index=False)
        e["config"] = exp
        e["objective"] = round(objective([R.trades(exp, f) for f in FILLS], min_trades=10), 3)
        res["experiments"][name] = e
        print(f"{name:45s} base {e['base']['trades']:3d} tr  net {e['base']['net']:7d}  exp {e['base']['exp_R']:+.3f}R   "
              f"cons {e['conservative']['trades']:3d} tr  net {e['conservative']['net']:7d}  ({time.time() - t0:.0f}s)")

    # 2b. Scalper V2 at the current settings (its own engine)
    e = {f: metrics(v2_trades(data, f)) for f in FILLS}
    e["config"] = dict(strategy="Scalper V2 (scalp_v2_backtest engine)", sl=15, tg=30, be_r=1.0, daily_loss=2500, hold=30)
    e["objective"] = round(objective([v2_trades(data, f) for f in FILLS], min_trades=10), 3)
    res["experiments"]["H0 Scalper V2 (active rule) · 15/30"] = e
    print(f"H0 Scalper V2 · 15/30  base {e['base']['trades']} tr net {e['base']['net']}  cons {e['conservative']['trades']} tr net {e['conservative']['net']}")

    # 3. G: combinations of components that beat the baseline on BOTH fill models
    base = res["experiments"]["A baseline (volume rule, 5/10)"]
    passed = [n for n, e in res["experiments"].items() if n[0] in "BCDEF"
              and all(e[f]["exp_R"] > base[f]["exp_R"] and e[f]["trades"] >= 10 for f in FILLS)]
    res["passed_components"] = passed
    entries = [n for n in passed if n[0] in "BCE"]
    best_entry = max(entries, key=lambda n: res["experiments"][n]["objective"]) if entries else None
    exits_ = [n for n in passed if n[0] == "F"]
    best_exit = max(exits_, key=lambda n: res["experiments"][n]["objective"]) if exits_ else None
    combos = {}
    if best_entry:
        cfg = dict(EXPERIMENTS[best_entry])
        if "D baseline + regime filter" in passed:
            cfg["regime"] = REG
        if best_exit:
            cfg["exit"] = dict(cfg.get("exit", {}), **EXPERIMENTS[best_exit].get("exit", {}))
        combos["G selected combination"] = cfg
    # exploratory combos, always shown, clearly labelled as not selected by the rule
    combos["G* sweep + regime filter (exploratory)"] = dict(strategy="sweep", regime=REG)
    combos["G* breakout-retest + regime filter (exploratory)"] = dict(strategy="retest", regime=REG)
    for name, exp in combos.items():
        e = {f: metrics(R.trades(exp, f)) for f in FILLS + ("v2_model",)}
        e["config"] = exp
        e["objective"] = round(objective([R.trades(exp, f) for f in FILLS], min_trades=10), 3)
        e["by_session"] = by(R.trades(exp, "base"), "tod")
        e["by_regime"] = by(R.trades(exp, "base"), "regime")
        res["experiments"][name] = e
    if not best_entry:
        res["notes"].append("No entry component beat the baseline's expectancy under both base and conservative fills "
                            "with >= 10 trades, so no combination was selected.")

    # 4. walk-forward, sensitivity, volume value
    folds, wf, wf_rows = walk_forward(R)
    res["walk_forward"] = dict(folds=[dict(train=f"{a[0]}→{a[-1]}", test=f"{b[0]}→{b[-1]}") for a, b in folds],
                               oos=wf, rows=wf_rows)
    res["sensitivity"] = sensitivity(R)
    q, vv = volume_value(M)
    res["volume_value"] = dict(quintiles=q, **vv)
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    write_md(res)
    print(f"done in {time.time() - t0:.0f}s → {OUT}")
    return res


def write_md(res):
    E = res["experiments"]
    L = [f"# Scalper strategy lab — results\n",
         f"NIFTY · {res['period']} ({res['days']} trading days) · 1 lot · dataset `{res['dataset']['name']}` "
         f"(sha256 {res['dataset']['sha256']}) · generated {res['generated']}\n",
         "Paper research only. Option prices are modelled (Black-Scholes, IV = previous day's India VIX); volume is "
         "NIFTYBEES.NS; there are no historical option quotes, so spread is a modelled assumption.\n",
         "## 1. Baseline reproduction\n", "| fills | old engine trades | old net | lab engine trades | lab net | identical trades |",
         "|---|---|---|---|---|---|"]
    for f, r in res["reproduction"].items():
        L.append(f"| {f} | {r['old_trades']} | {r['old_net']:,} | {r['new_trades']} | {r['new_net']:,} | {r['identical_trades']} |")
    L += ["\n## 2. All experiments (lab engine)\n",
          "| experiment | fills | trades | win % | net ₹ | PF | exp ₹/trade (95% CI) | exp R | avg win | avg loss | max DD | P(total>0) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, e in E.items():
        for f in ("base", "conservative"):
            m = e[f]
            L.append(f"| {n} | {f} | {m['trades']} | {m['win_rate']} | {m['net']:,} | {m['pf']} | {m['exp_rs']} "
                     f"({m['ci_low']} to {m['ci_high']}) | {m['exp_R']:+.3f} | {m['avg_win']} | {m['avg_loss']} | "
                     f"{m['max_dd']:,} | {m['p_positive']} |")
    L += ["\n## 3. Walk-forward (anchored; chosen on earlier days, scored on unseen later days)\n",
          "Folds: " + "; ".join(f"train {x['train']} / test {x['test']}" for x in res["walk_forward"]["folds"]) + "\n",
          "| family | fills | OOS trades | OOS net ₹ | OOS exp R | PF | P(total>0) |", "|---|---|---|---|---|---|---|"]
    for fam, d in res["walk_forward"]["oos"].items():
        for f, m in d.items():
            L.append(f"| {fam} | {f} | {m['trades']} | {m['net']:,} | {m['exp_R']:+.3f} | {m['pf']} | {m['p_positive']} |")
    L += ["\n## 4. Components that beat the baseline on both fill models\n",
          (", ".join(res["passed_components"]) or "_none_") + "\n"] + [f"- {x}" for x in res["notes"]]
    v = res["volume_value"]
    L += ["\n## 5. Does NIFTYBEES volume carry information about NIFTY?\n",
          f"{v['n']} completed 1-min candles. Correlation of relative volume with the size of the next 5-min move: "
          f"{v['corr_abs']}; with continuation in the candle's direction: {v['corr_cont']}.\n",
          "| rvol quintile | median rvol | candles | next-5-min |move| pts | continuation pts |", "|---|---|---|---|---|"]
    for q in v["quintiles"]:
        L.append(f"| {q['q']} | {q['rvol_mid']} | {q['n']} | {q['abs_move_pts']} | {q['continuation_pts']} |")
    L.append("\nNIFTY futures / option volume: not available historically on the free feed, and this Kite key gets "
             "PermissionException for quotes/historical data, so the comparison with futures volume could not be run.\n")
    with open(os.path.join(OUT, "RESULTS.md"), "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
