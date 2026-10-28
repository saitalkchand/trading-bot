"""
Trader agent.

Turns the strategy signal plus the debate into a concrete call:
  BUY CE (bullish) · BUY PE (bearish — this bot buys puts, it never sells options) · HOLD.

  * No strategy signal → HOLD, with the debate's lean shown as a stance. The trader never
    invents a trade; only the strategy's entry rules can start one.
  * Signal, and the debate went against it by TRADER_BLOCK_MARGIN or more → HOLD.
  * Otherwise → BUY that side, with a conviction (0..1) for the log and dashboard.
The risk manager still has the final say.
"""
from . import clip


def decide(sig, deb, cfg):
    g = (lambda k: cfg[k]) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k))
    m = deb["margin"]
    stance = "buy (calls)" if m >= 0.15 else "sell (puts)" if m <= -0.15 else "neutral"
    if not sig:
        return dict(action="HOLD", stance=stance, conviction=round(abs(m), 2),
                    reason=f"No strategy signal. Debate leans {stance} ({deb['verdict']}, margin {m:+.2f}).")
    side = sig["side"]
    support = m if side == "CE" else -m
    conviction = clip(0.5 + support - 0.15 * len(deb["flags"]), 0, 1)
    if support <= -g("TRADER_BLOCK_MARGIN"):
        top = deb["challenges"][0][0] if deb["challenges"] else "the other side's case"
        return dict(action="HOLD", stance=stance, conviction=round(conviction, 2), side=side,
                    reason=f"{side} signal overruled: debate margin {m:+.2f} against it. Strongest objection — {top}")
    return dict(action=f"BUY {side}", stance=stance, conviction=round(conviction, 2), side=side,
                reason=f"{sig['why']}; debate {deb['verdict']} (margin {m:+.2f})"
                       + (f"; flags: {'; '.join(deb['flags'])}" if deb["flags"] else ""))
