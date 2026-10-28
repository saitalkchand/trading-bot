"""
Risk manager / compliance agent — the last gate before an order is sent.

Every check must pass, or the trade is vetoed:
  1. Hard stop-loss   the trade has a defined index stop (and, in live mode, the bot places
                      an exchange-side disaster stop right after the fill)
  2. Daily lock       no daily-loss / max-trades / losses-in-a-row lock is active
  3. Exposure         open positions below MAX_OPEN_POSITIONS
  4. Premium cap      premium × qty ≤ MAX_PREMIUM_PER_TRADE
  5. Loss budget      estimated loss if the stop is hit ≤ what's left of MAX_DAILY_LOSS today
  6. Safety vetoes    crude-oil, news and global-markets vetoes, high-impact event days (strategy.agent_veto),
                      plus heavyweight results days when NEWS_BLOCK_ON_EVENT is on
"""
import strategy

ITM_DELTA = 0.7  # an ITM option moves ~0.6–0.75 × the index; used to estimate the loss at the stop


def review(sig, qty, premium, risk_state, open_positions, crude, news, fundamental, cfg, glob=None):
    g = (lambda k: cfg[k]) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k))
    checks = []

    def check(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    check("Hard stop-loss", sig.get("sl") and sig.get("risk", 0) > 0,
          f"index stop {sig['sl']:.1f} ({sig['risk']:.1f} pts away)" if sig.get("sl") else "no stop defined")
    check("Daily lock", not risk_state.get("locked"), risk_state.get("reason") or "no lock active")
    check("Exposure", open_positions < g("MAX_OPEN_POSITIONS"),
          f"{open_positions} of {g('MAX_OPEN_POSITIONS')} positions open")
    cost = premium * qty
    check("Premium cap", cost <= g("MAX_PREMIUM_PER_TRADE"),
          f"₹{cost:,.0f} of ₹{g('MAX_PREMIUM_PER_TRADE'):,.0f} allowed")
    est = qty * ITM_DELTA * sig.get("risk", 0)
    left = g("MAX_DAILY_LOSS") + risk_state.get("pnl", 0)
    check("Loss budget", est <= left, f"≈₹{est:,.0f} at the stop vs ₹{max(left, 0):,.0f} left today")
    veto = strategy.agent_veto(sig["side"], crude, news, cfg, glob)
    if not veto and g("NEWS_BLOCK_ON_EVENT") and fundamental and fundamental.get("results_today"):
        veto = "results day for " + ", ".join(fundamental["results_today"][:3])
    check("Safety vetoes", not veto, veto or "crude, news, global and event checks clear")

    failed = [c for c in checks if not c["ok"]]
    return dict(approved=not failed, checks=checks,
                reason="; ".join(f"{c['name']}: {c['detail']}" for c in failed) if failed else "all checks passed")
