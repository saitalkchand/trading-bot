"""
Trade-management policies on the OPTION PREMIUM of a bought CE/PE (the bot only buys options, so the
position always gains when the premium rises — for a PE too).

Every change goes through move_stop / move_target, which
  * round to the 0.05 tick,
  * refuse a lower stop (would add risk) and a no-op/duplicate change,
  * append an audit event: time, rule, old → new, and the change in rupees at risk (negative = less risk).
Changes made on a candle take effect from the NEXT candle (decided on the close, like a live bot).

Index-structure stops are converted to premium with the option's delta at entry, then widened by
STRUCT_DELTA_BUF to allow for gamma/IV changes; trades whose premium stop falls outside
[STRUCT_MIN_SL, STRUCT_MAX_SL] are rejected rather than resized.
Partial exits are not modelled: the scalper's order manager exits the whole quantity, and 1 lot can't be split.
"""
TICK = 0.05

EXIT_DEFAULTS = dict(
    EXIT_POLICY="fixed",        # fixed | vol | structure
    SL_POINTS=5.0, TG_POINTS=10.0, BE_AT_POINTS=5.0,          # fixed (the existing benchmark)
    VOL_ATR_MULT=0.8, VOL_MIN_SL=4.0, VOL_MAX_SL=10.0, VOL_RR=2.0,
    STRUCT_DELTA_BUF=1.15, STRUCT_MIN_SL=3.0, STRUCT_MAX_SL=12.0, STRUCT_RR=2.0,
    BE_AT_R=None,               # None = use BE_AT_POINTS for fixed, 1.0R otherwise; 0 = off
    TRAIL="off",                # off | atr | lock
    TRAIL_START_R=1.0, TRAIL_ATR_MULT=1.0,
    LOCK_STEPS=((1.0, 0.0), (1.5, 0.5)),       # at +1R lock cost, at +1.5R lock +0.5R
    EXTEND=False, EXTEND_AT=0.8, EXTEND_BY_R=0.5, EXTEND_LOCK_R=0.5, EXTEND_MAX=1,
)


def tick(x, on=True):
    return round(round(x / TICK) * TICK, 2) if on else x


def move_stop(pos, new, rule, t):
    new = tick(new, pos.get("round", True))
    if new <= pos["stop"]:
        return False
    pos["events"].append(dict(t=str(t), what="stop", rule=rule, old=pos["stop"], new=new,
                              risk_change=round((new - pos["stop"]) * -pos["qty"], 2)))
    pos["stop"] = new
    return True


def move_target(pos, new, rule, t):
    new = tick(new, pos.get("round", True))
    if new == pos["target"]:
        return False
    pos["events"].append(dict(t=str(t), what="target", rule=rule, old=pos["target"], new=new, risk_change=0.0))
    pos["target"] = new
    return True


def initial(pos, sig, ctx, cfg):
    """Set stop/target at entry. Returns a rejection reason or None.
    ctx: entry premium (buy), delta, idx_atr, cost_unit."""
    g = lambda k: cfg.get(k, EXIT_DEFAULTS[k])
    pol, buy, delta = g("EXIT_POLICY"), ctx["buy"], abs(ctx["delta"])
    if pol == "fixed":
        sl, tg = g("SL_POINTS"), g("TG_POINTS")
    elif pol == "vol":
        sl = min(max(g("VOL_ATR_MULT") * ctx["idx_atr"] * delta, g("VOL_MIN_SL")), g("VOL_MAX_SL"))
        tg = g("VOL_RR") * sl
    elif pol == "structure":
        if sig.get("idx_stop") is None:
            return "no structure level"
        sl = abs(sig["entry"] - sig["idx_stop"]) * delta * g("STRUCT_DELTA_BUF")
        if not g("STRUCT_MIN_SL") <= sl <= g("STRUCT_MAX_SL"):
            return f"structure stop {sl:.1f} premium pts outside {g('STRUCT_MIN_SL'):g}–{g('STRUCT_MAX_SL'):g}"
        tg = g("STRUCT_RR") * sl
    else:
        raise ValueError(pol)
    r = pos.get("round", True)
    pos.update(sl=sl, tg=tg, stop=tick(buy - sl, r), target=tick(buy + tg, r), best=buy, be=False, ext=0, events=[])
    be = g("BE_AT_R")
    pos["be_at"] = (g("BE_AT_POINTS") if pol == "fixed" else sl) if be is None else be * sl
    return None


def update(pos, hi_p, close_p, F, t, cfg):
    """After a candle with no exit: break-even, trailing, target extension. Deterministic rules only."""
    g = lambda k: cfg.get(k, EXIT_DEFAULTS[k])
    buy, R = pos["buy"], pos["sl"]
    pos["best"] = max(pos["best"], hi_p)
    gain = pos["best"] - buy
    if pos["be_at"] and not pos["be"] and gain >= pos["be_at"]:
        pos["be"] = True
        move_stop(pos, buy + pos["cost_unit"], "break-even (to cost incl. charges)", t)
    tr = g("TRAIL")
    if tr == "atr" and gain >= g("TRAIL_START_R") * R:
        move_stop(pos, pos["best"] - g("TRAIL_ATR_MULT") * F.atr * abs(pos["delta"]), "ATR trail", t)
    elif tr == "lock":
        for at_r, lock_r in g("LOCK_STEPS"):
            if gain >= at_r * R:
                move_stop(pos, buy + max(lock_r * R, pos["cost_unit"] if lock_r == 0 else 0), f"profit lock at +{at_r}R", t)
    if g("EXTEND") and pos["ext"] < g("EXTEND_MAX") and pos["best"] >= buy + g("EXTEND_AT") * (pos["target"] - buy):
        b = F.bar
        d = 1 if pos["side"] == "CE" else -1
        momentum = (b["c"] - b["o"]) * d > 0 and (b["c"] - F.ema[9]) * d > 0 and (b["c"] - F.vwap) * d > 0
        if momentum:
            pos["ext"] += 1
            move_stop(pos, buy + g("EXTEND_LOCK_R") * R, "lock before target extension", t)
            move_target(pos, pos["target"] + g("EXTEND_BY_R") * R, "target extension (momentum + structure)", t)
