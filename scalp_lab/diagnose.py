"""
Why do losing trades lose? Post-trade analysis — it MAY look at candles after the exit, because it judges
trades that are already closed; nothing here feeds back into an entry decision.

Each losing trade gets every cause that applies (so counts overlap) plus one primary cause (first match,
in the order below):
  cost-flipped     would have made money at mid prices; spread/slippage/charges turned it into a loss
  stop at cost     break-even exit that still lost (costs/slippage on the way out)
  false breakout   the index closed back inside the broken 5-candle range before the exit
  noise stop       stopped within 3 min, and the target premium was reached within 10 min after the stop
  chop             entered in a RANGE / LOW_ACTIVITY regime
  late entry       signal candle > 1.5 x ATR, or entry > 0.15% from proxy VWAP (chasing)
  time stop        nothing happened within the hold limit
  other
"""
from collections import Counter, defaultdict

from backtest import bs_price, next_expiry, years_to

CAUSES = ["cost-flipped", "stop at cost", "false breakout", "noise stop", "chop", "late entry", "time stop", "other"]


def causes(tr, M, box_n=5):
    idx = {b["t"]: i for i, b in enumerate(M.bars)}
    out = []
    for r in tr.itertuples():
        if r.net >= 0:
            out.append(([], "win")); continue
        flags = []
        if r.exit_mid - r.entry_mid > 0:
            flags.append("cost-flipped")
        if "cost" in r.reason:
            flags.append("stop at cost")
        i = idx.get(r.t_sig)
        d = 1 if r.side == "CE" else -1
        if i is not None and i > box_n:
            box = M.bars[i - box_n:i]
            level = max(b["h"] for b in box) if d > 0 else min(b["l"] for b in box)
            j_exit = idx.get(r.t_exit, idx.get(r.t_exit - (r.t_exit - r.t_exit.replace(second=0))))
            held = [b for b in M.bars[i + 1:(j_exit or i + 1) + 1]]
            if any((b["c"] - level) * d < 0 for b in held):
                flags.append("false breakout")
        if "stop" in r.reason and "cost" not in r.reason and r.hold_min <= 3 and i is not None:
            exp = next_expiry(r.t_entry, M.spec["expiry_weekday"])
            iv = M.iv(r.date)
            j = idx.get(r.t_exit) or idx.get(r.t_exit.replace(second=0))
            if j is not None:
                later = M.bars[j:j + 10]
                best = max((b["h"] if d > 0 else b["l"]) * d for b in later) * d if later else None
                if best is not None:
                    p_best = bs_price(best, r.strike, years_to(exp, r.t_exit), iv, r.side)
                    if p_best >= r.entry_mid + r.tg_pts:
                        flags.append("noise stop")
        if r.regime in ("RANGE", "LOW_ACTIVITY"):
            flags.append("chop")
        if r.sig_range_atr > 1.5 or abs(r.vwap_dist_pct) > 0.15:
            flags.append("late entry")
        if r.reason == "time stop":
            flags.append("time stop")
        primary = next((c for c in CAUSES if c in flags), "other")
        out.append((flags, primary))
    return out


def summary(tr, M):
    res = causes(tr, M)
    tagged = Counter(); primary = Counter(); money = defaultdict(float)
    for (flags, prim), net in zip(res, tr.net):
        if prim == "win":
            continue
        primary[prim] += 1
        money[prim] += net
        for f in flags:
            tagged[f] += 1
    losers = int((tr.net < 0).sum())
    rows = [dict(cause=c, primary=primary.get(c, 0), primary_net=round(money.get(c, 0)), any=tagged.get(c, 0),
                 share_of_losers=f"{100 * tagged.get(c, 0) / losers:.0f}%" if losers else "0%") for c in CAUSES]
    costs = dict(trades=len(tr), gross_at_mid=round((tr.gross + tr.slippage).sum()), slippage=round(tr.slippage.sum()),
                 charges=round(tr.charges.sum()), net=round(tr.net.sum()),
                 cost_per_trade=round((tr.slippage + tr.charges).mean()) if len(tr) else 0,
                 avg_risk_per_trade=round((tr.sl_pts * tr.qty).mean()) if len(tr) else 0)
    return rows, costs
