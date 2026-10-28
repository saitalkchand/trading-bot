"""Performance metrics, Monte Carlo and a robust selection objective."""
import math

import numpy as np
import pandas as pd

CAPITAL = 300_000          # assumed account size for % figures, Sharpe/Sortino/Calmar (₹)
BUCKETS = [("09:15–09:30", 555, 570), ("09:30–10:00", 570, 600), ("10:00–11:00", 600, 660),
           ("11:00–12:00", 660, 720), ("12:00–13:00", 720, 780), ("13:00–14:00", 780, 840),
           ("14:00–15:00", 840, 900), ("15:00–15:30", 900, 931)]


def bucket(tod):
    for name, a, b in BUCKETS:
        if a < tod <= b or (a == 555 and tod == 555):
            return name
    return "other"


def daily(tr, days):
    s = tr.groupby("date").net.sum() if len(tr) else pd.Series(dtype=float)
    return pd.Series([float(s.get(d, 0.0)) for d in days], index=list(days))


def streaks(x):
    best_w = best_l = cw = cl = 0
    for v in x:
        if v > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        best_w, best_l = max(best_w, cw), max(best_l, cl)
    return best_w, best_l


def max_dd(series):
    eq = np.cumsum(series)
    peak = np.maximum.accumulate(np.concatenate([[0], eq]))[1:]
    return float((eq - peak).min()) if len(eq) else 0.0


def summary(tr, days, capital=CAPITAL):
    d = daily(tr, days)
    n = len(tr)
    if not n:
        return dict(trades=0, win_rate=0, net=0, avg_trade=0, pf=0, expectancy_R=0, max_dd=0, sharpe=0,
                    sortino=0, recovery=0, worst_day=0, days=len(days))
    w, l = tr[tr.net > 0], tr[tr.net <= 0]
    gw, gl = w.net.sum(), -l.net.sum()
    dd = max_dd(d.values)
    mu, sd = d.mean(), d.std(ddof=1) if len(d) > 1 else 0
    dn = d[d < 0]
    dsd = math.sqrt((dn ** 2).sum() / len(d)) if len(d) else 0
    ann = d.mean() * 252
    ws, ls = streaks(tr.sort_values(["date", "entry_time"]).net.values)
    return dict(
        trades=n, win_rate=100 * len(w) / n, loss_rate=100 * len(l) / n,
        gross=float(tr.gross.sum()), net=float(tr.net.sum()), charges=float(tr.charges.sum()),
        avg_trade=float(tr.net.mean()), avg_win=float(w.net.mean()) if len(w) else 0.0,
        avg_loss=float(l.net.mean()) if len(l) else 0.0,
        pf=float(gw / gl) if gl > 0 else float("inf"),
        expectancy_R=float(tr.R.mean()), reward_risk=float(w.net.mean() / -l.net.mean()) if len(w) and len(l) and l.net.mean() < 0 else 0.0,
        max_dd=dd, max_dd_pct=100 * dd / capital, recovery=float(tr.net.sum() / -dd) if dd < 0 else float("inf"),
        sharpe=float(mu / sd * math.sqrt(252)) if sd > 0 else 0.0,
        sortino=float(mu / dsd * math.sqrt(252)) if dsd > 0 else 0.0,
        calmar=float((ann / capital) / (-dd / capital)) if dd < 0 else float("inf"),
        win_days=int((d > 0).sum()), loss_days=int((d < 0).sum()), days=len(days),
        win_streak=ws, loss_streak=ls, best_day=float(d.max()), worst_day=float(d.min()),
        stop_rate=100 * tr.reason.eq("stop-loss").mean(),
    )


def breakdown(tr, key):
    if not len(tr):
        return pd.DataFrame(columns=["trades", "win_rate", "net", "avg", "avg_win", "avg_loss", "exp_R", "pf", "max_dd"])
    g = tr.groupby(key)
    out = pd.DataFrame(dict(
        trades=g.size(), win_rate=g.net.apply(lambda x: 100 * (x > 0).mean()), net=g.net.sum(),
        avg=g.net.mean(), avg_win=g.net.apply(lambda x: x[x > 0].mean()), avg_loss=g.net.apply(lambda x: x[x <= 0].mean()),
        exp_R=g.R.mean(), pf=g.net.apply(lambda x: x[x > 0].sum() / -x[x <= 0].sum() if (x <= 0).any() and x[x <= 0].sum() < 0 else np.inf),
        max_dd=g.net.apply(lambda x: max_dd(x.values))))
    return out.round(2)


def objective(tr, min_trades=6):
    """Robust, scale-free selection score — set BEFORE testing:
    SQN-style edge (mean R / sd R × √n, n capped at 30) + recovery in R (capped at 3).
    Needs >= min_trades and PF >= 1, otherwise heavily penalised. Never total P&L."""
    n = len(tr)
    if n < min_trades:
        return -9.0
    r = tr.R.values
    sd = r.std(ddof=1) or 1e-9
    sqn = r.mean() / sd * math.sqrt(min(n, 30))
    dd = -max_dd(r)
    rec = min(3.0, r.sum() / dd) if dd > 0 else 3.0
    pf = r[r > 0].sum() / -r[r <= 0].sum() if (r <= 0).any() and r[r <= 0].sum() < 0 else 9
    return sqn + rec - (3 if pf < 1 else 0)


def monte_carlo(tr, days, n_sims=10000, seed=7, month_days=21):
    """1) Trade-order permutation → drawdown distribution.
    2) Daily-P&L bootstrap → distribution of 21-day months and of a same-length period."""
    rng = np.random.default_rng(seed)
    pnl = tr.sort_values(["date", "entry_time"]).net.values.astype(float)
    if len(pnl) < 5:
        return {}
    dds = np.array([max_dd(rng.permutation(pnl)) for _ in range(n_sims)])
    d = daily(tr, days).values
    months = np.array([rng.choice(d, month_days, replace=True).sum() for _ in range(n_sims)])
    period = np.array([rng.choice(d, len(d), replace=True).sum() for _ in range(n_sims)])
    return dict(
        dd_actual=max_dd(pnl), dd_median=float(np.median(dds)), dd_p95=float(np.percentile(dds, 5)),
        dd_worst=float(dds.min()),
        month_median=float(np.median(months)), month_p5=float(np.percentile(months, 5)),
        month_p95=float(np.percentile(months, 95)), p_neg_month=float((months < 0).mean() * 100),
        period_median=float(np.median(period)), period_p5=float(np.percentile(period, 5)),
        p_loss_period=float((period < 0).mean() * 100))
