"""
Scalper entry strategies as incremental detectors. Call update(F) once per COMPLETED candle, after
F (features.Features) has been updated with it. A detector only ever sees candles up to the current one,
so it cannot use the future; a signal always refers to an entry at/after this candle's close.

Every setup is recorded in .log with a status:
  provisional  — the first condition happened (a sweep, a breakout); waiting for confirmation
  confirmed    — all entry conditions passed on this candle → a signal is returned
  rejected     — confirmation happened but a filter failed (reason stored)
  expired      — no confirmation within the allowed candles
  invalidated  — price did something that kills the idea (reason stored)
  superseded   — a newer setup of the same side replaced it

Signal: dict(side, kind, entry, idx_stop, idx_target, why, setup)
  idx_stop   index level that invalidates the idea (structure stop)
  idx_target nearest opposing liquidity, or None
"""
import strategy as legacy

SWEEP_DEFAULTS = dict(
    SWEEP_MIN_PEN_ATR=0.10,     # a sweep must trade at least this far beyond the level (not every touch)
    SWEEP_MAX_PEN_ATR=1.50,     # ... and not further: deeper is a breakdown, not a sweep
    SWEEP_RECLAIM_BARS=3,       # candles after the sweep candle allowed for the reclaim
    SWEEP_SAME_BAR=False,       # allow the sweep candle itself to be the reclaim
    SWEEP_BODY_MIN=0.5,         # confirmation candle body / range
    SWEEP_DISP_ATR=0.5,         # confirmation candle body >= this x ATR (displacement)
    SWEEP_HTF="not_against",    # "with" | "not_against" | "off"  (15-min EMA-20 of completed candles)
    SWEEP_VWAP="off",           # "with" (close on the trade side of VWAP) | "off"
    SWEEP_RVOL_MIN=0.0,         # min relative volume on the sweep or the confirmation candle (0 = off)
    SWEEP_MIN_REWARD_R=1.5,     # nearest opposing liquidity must be >= this x the index risk away
    SWEEP_STOP_BUF_ATR=0.10,    # stop beyond the sweep extreme
)

BRT_DEFAULTS = dict(
    BRT_RANGE_N=10,             # completed candles that define the range
    BRT_BREAK_BUF_ATR=0.05,     # close must clear the range by this x ATR
    BRT_BODY_MIN=0.5,           # breakout candle body / range
    BRT_RVOL_MIN=0.0,           # relative volume on the breakout candle (0 = off)
    BRT_TREND_FILTER=True,      # close on the trade side of proxy VWAP and EMA-21 (the existing filters)
    BRT_HTF="not_against",
    BRT_RETEST=True,            # False = enter on the breakout close (no-retest variant)
    BRT_RETEST_BARS=6,          # candles allowed for the retest
    BRT_RETEST_TOL_ATR=0.25,    # a low within level + tol counts as touching the level
    BRT_HOLD_TOL_ATR=0.25,      # a close below level - tol fails the breakout
    BRT_MIN_REWARD_R=1.5,
    BRT_STOP_BUF_ATR=0.10,
)


def _g(cfg, defaults):
    return lambda k: cfg.get(k, defaults[k])


class _Base:
    name = "base"

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.log = []
        self.active = []

    def _rec(self, setup, status, reason=""):
        setup = dict(setup, status=status, reason=reason)
        self.log.append(setup)
        return setup

    def _htf_ok(self, F, side, mode):
        b = F.htf_bias()
        d = 1 if side == "CE" else -1
        if mode == "with":
            return b == d
        if mode == "not_against":
            return b != -d
        return True

    def _reward_ok(self, F, side, entry, stop, min_r):
        risk = abs(entry - stop)
        obj = F.opposing_liquidity(side, entry)
        if obj is None:
            return True, None, "no opposing level (open air)"
        room = abs(obj - entry)
        return room >= min_r * risk, obj, f"room {room:.1f} vs risk {risk:.1f}"


class VolumeBaseline(_Base):
    """The existing volume-breakout rule, called unchanged (strategy.signal_volume) on the same candles."""
    name = "volume baseline"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.hist = []

    def update(self, F):
        b = F.bar
        self.hist.append(dict(o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b.get("v"), day=b["t"].date() if b.get("t") else None))
        self.hist = self.hist[-400:]
        s = legacy.signal_volume(self.hist, self.cfg)
        if not s:
            return None
        disp = self.cfg.get("VOL_DISP_ATR", 0)
        if disp and abs(b["c"] - b["o"]) < disp * F.atr:
            self._rec(dict(t=b.get("t"), side=s["side"], kind="volume"), "rejected", f"body < {disp} ATR")
            return None
        setup = self._rec(dict(t=b.get("t"), side=s["side"], kind="volume", level=None), "confirmed", s["why"])
        return dict(side=s["side"], kind="volume breakout", entry=b["c"], idx_stop=s["sl"], idx_target=None,
                    why=s["why"], setup=setup)


class SweepReversal(_Base):
    """Liquidity sweep → reclaim → displacement confirmation. CE after a sweep of a low, PE after a high."""
    name = "liquidity sweep reversal"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.g = _g(self.cfg, SWEEP_DEFAULTS)
        self.pending = {"CE": None, "PE": None}
        self.used = {"CE": set(), "PE": set()}     # levels already swept: one sweep → at most one setup

    def update(self, F):
        if not F.ready():
            return None
        out = None
        for side in ("CE", "PE"):
            sig = self._advance(F, side)
            out = out or sig
            if not out:
                out = self._new_sweep(F, side)
        self.active = [dict(p) for p in self.pending.values() if p]
        return out

    def _pen(self, side, level, extreme):
        return (level - extreme) if side == "CE" else (extreme - level)

    def _new_sweep(self, F, side):
        g, b, a = self.g, F.bar, F.atr
        extreme = b["l"] if side == "CE" else b["h"]
        best = None
        for lv in F.levels(side):
            key = round(lv["price"], 2)
            if key in self.used[side]:
                continue
            pen = self._pen(side, lv["price"], extreme)
            if pen >= g("SWEEP_MIN_PEN_ATR") * a:
                self.used[side].add(key)
                if pen <= g("SWEEP_MAX_PEN_ATR") * a and (best is None or pen < self._pen(side, best["price"], extreme)):
                    best = lv
        if not best:
            return None
        if self.pending[side]:
            self._rec(self.pending[side], "superseded", "a newer sweep")
        setup = dict(t=b.get("t"), i_sweep=F.i, side=side, kind="sweep", level=best["price"], level_kind=best["kind"],
                     extreme=extreme, sweep_rvol=F.rvol)
        self.pending[side] = setup
        self._rec(setup, "provisional", f"swept {best['kind']} {best['price']:.1f} by {self._pen(side, best['price'], extreme):.1f} pts")
        if g("SWEEP_SAME_BAR"):
            return self._advance(F, side, same_bar=True)
        return None

    def _advance(self, F, side, same_bar=False):
        s = self.pending[side]
        if not s or (s["i_sweep"] == F.i and not same_bar):
            return None
        g, b, a = self.g, F.bar, F.atr
        d = 1 if side == "CE" else -1
        if F.i - s["i_sweep"] > g("SWEEP_RECLAIM_BARS"):
            self.pending[side] = None
            self._rec(s, "expired", "no reclaim in time")
            return None
        ext = b["l"] if side == "CE" else b["h"]
        if (s["extreme"] - ext) * d > 0:                      # went further beyond the level
            s["extreme"] = ext
            if self._pen(side, s["level"], ext) > g("SWEEP_MAX_PEN_ATR") * a:
                self.pending[side] = None
                self._rec(s, "invalidated", "kept going: breakdown, not a sweep" if side == "CE" else "kept going: breakout, not a sweep")
                return None
        reclaimed = (b["c"] - s["level"]) * d > 0
        strong = (b["c"] - b["o"]) * d > 0 and F.body_ratio() >= g("SWEEP_BODY_MIN") \
            and abs(b["c"] - b["o"]) >= g("SWEEP_DISP_ATR") * a
        if not (reclaimed and strong):
            return None
        self.pending[side] = None
        stop = s["extreme"] - d * g("SWEEP_STOP_BUF_ATR") * a
        why = []
        if not self._htf_ok(F, side, g("SWEEP_HTF")):
            why.append("15-min trend against")
        if g("SWEEP_VWAP") == "with" and (b["c"] - F.vwap) * d <= 0:
            why.append("wrong side of VWAP")
        rv = max(x for x in (s.get("sweep_rvol"), F.rvol, 0) if x is not None)
        if g("SWEEP_RVOL_MIN") and rv < g("SWEEP_RVOL_MIN"):
            why.append(f"volume {rv:.1f}x < {g('SWEEP_RVOL_MIN')}x")
        ok, obj, room = self._reward_ok(F, side, b["c"], stop, g("SWEEP_MIN_REWARD_R"))
        if not ok:
            why.append("not enough room: " + room)
        setup = dict(s, t_confirm=b.get("t"), entry=b["c"], invalidation=stop, objective=obj)
        if why:
            self._rec(setup, "rejected", "; ".join(why))
            return None
        self._rec(setup, "confirmed", room)
        return dict(side=side, kind="sweep reversal", entry=b["c"], idx_stop=stop, idx_target=obj, setup=setup,
                    why=f"swept {s['level_kind']} {s['level']:.1f}, reclaimed with a {F.body_ratio():.0%} body; {room}")


class BreakoutRetest(_Base):
    """Range breakout → (optional) retest of the broken level that holds → resumption → entry."""
    name = "breakout retest"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.g = _g(self.cfg, BRT_DEFAULTS)
        self.pending = {"CE": None, "PE": None}
        self.used = set()

    def update(self, F):
        if not F.ready():
            return None
        out = None
        for side in ("CE", "PE"):
            out = out or self._advance(F, side)
        if not out:
            out = self._new_breakout(F)
        self.active = [dict(p) for p in self.pending.values() if p]
        return out

    def _filters(self, F, side):
        g, b = self.g, F.bar
        d = 1 if side == "CE" else -1
        why = []
        if g("BRT_TREND_FILTER") and not ((b["c"] - F.vwap) * d > 0 and (b["c"] - F.ema[21]) * d > 0):
            why.append("not on the VWAP/EMA-21 side")
        if not self._htf_ok(F, side, g("BRT_HTF")):
            why.append("15-min trend against")
        return why

    def _new_breakout(self, F):
        g, b, a = self.g, F.bar, F.atr
        n = g("BRT_RANGE_N")
        prior = list(F.bars)[-1 - n:-1]
        if len(prior) < n:
            return None
        hi, lo = max(x["h"] for x in prior), min(x["l"] for x in prior)
        for side, level in (("CE", hi), ("PE", lo)):
            d = 1 if side == "CE" else -1
            if (b["c"] - level) * d < g("BRT_BREAK_BUF_ATR") * a or (b["c"] - b["o"]) * d <= 0:
                continue
            key = (side, round(level, 2))
            if key in self.used:
                continue
            self.used.add(key)
            setup = dict(t=b.get("t"), i_break=F.i, side=side, kind="breakout", level=level, break_close=b["c"],
                         extreme=b["l"] if side == "CE" else b["h"], touched=False, rvol=F.rvol)
            why = self._filters(F, side)
            if F.body_ratio() < g("BRT_BODY_MIN"):
                why.append(f"weak breakout candle (body {F.body_ratio():.0%})")
            if g("BRT_RVOL_MIN") and (F.rvol or 0) < g("BRT_RVOL_MIN"):
                why.append(f"volume {(F.rvol or 0):.1f}x < {g('BRT_RVOL_MIN')}x")
            if why:
                self._rec(setup, "rejected", "; ".join(why))
                continue
            if not g("BRT_RETEST"):
                stop = b["l"] - g("BRT_STOP_BUF_ATR") * a if side == "CE" else b["h"] + g("BRT_STOP_BUF_ATR") * a
                return self._confirm(F, setup, stop, "breakout close (no retest)")
            if self.pending[side]:
                self._rec(self.pending[side], "superseded", "a newer breakout")
            self.pending[side] = setup
            self._rec(setup, "provisional", f"closed {'above' if d > 0 else 'below'} the {n}-candle range {level:.1f}")
        return None

    def _advance(self, F, side):
        s = self.pending[side]
        if not s or s["i_break"] == F.i:
            return None
        g, b, a = self.g, F.bar, F.atr
        d = 1 if side == "CE" else -1
        lvl = s["level"]
        if (b["c"] - (lvl - d * g("BRT_HOLD_TOL_ATR") * a)) * d < 0:
            self.pending[side] = None
            self._rec(s, "invalidated", "closed back inside the range (failed breakout)")
            return None
        if F.i - s["i_break"] > g("BRT_RETEST_BARS"):
            self.pending[side] = None
            self._rec(s, "expired", "no retest in time" if not s["touched"] else "no resumption after the retest")
            return None
        near = b["l"] if side == "CE" else b["h"]
        s["extreme"] = min(s["extreme"], b["l"]) if side == "CE" else max(s["extreme"], b["h"])
        if not s["touched"]:
            if (near - (lvl + d * g("BRT_RETEST_TOL_ATR") * a)) * d <= 0:
                s["touched"], s["i_touch"], s["touch_hi"], s["touch_lo"] = True, F.i, b["h"], b["l"]
            return None
        # resumption: closes beyond the retest candle in the breakout direction
        ref = s["touch_hi"] if side == "CE" else s["touch_lo"]
        if (b["c"] - ref) * d > 0 and (b["c"] - b["o"]) * d > 0:
            self.pending[side] = None
            why = self._filters(F, side)
            if why:
                self._rec(s, "rejected", "at resumption: " + "; ".join(why))
                return None
            stop = s["extreme"] - d * g("BRT_STOP_BUF_ATR") * a
            return self._confirm(F, s, stop, "retest held, resumed")
        return None

    def _confirm(self, F, s, stop, how):
        b = F.bar
        ok, obj, room = self._reward_ok(F, s["side"], b["c"], stop, self.g("BRT_MIN_REWARD_R"))
        setup = dict(s, t_confirm=b.get("t"), entry=b["c"], invalidation=stop, objective=obj)
        if not ok:
            self._rec(setup, "rejected", "not enough room: " + room)
            return None
        self._rec(setup, "confirmed", f"{how}; {room}")
        return dict(side=s["side"], kind="breakout retest" if self.g("BRT_RETEST") else "breakout", entry=b["c"],
                    idx_stop=stop, idx_target=obj, setup=setup,
                    why=f"{how} at {s['level']:.1f}; {room}")


STRATEGIES = {"volume": VolumeBaseline, "sweep": SweepReversal, "retest": BreakoutRetest}
