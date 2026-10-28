"""
Run a lab strategy inside the live/paper scalper with the exact code the backtest used.

Feed it the scalper's completed candles after every candle close; it updates the incremental features and the
detector once per new candle (by timestamp) and returns a signal only if the NEWEST candle confirmed one.
Experimental strategies are paper-only: scalper.py refuses to start them in live mode.
Live note: NIFTYBEES volume is fetched from Yahoo and can lag a minute, so the newest candle may have no
volume yet (the proxy VWAP then gives it no weight). The backtest had complete volume.
"""
from .features import Features
from .regime import classify
from .strategies import STRATEGIES

EXPERIMENTAL = ("sweep", "retest")
LABELS = {"sweep": "Liquidity sweep reversal", "retest": "Breakout-retest"}


class LabLive:
    def __init__(self, key, cfg=None):
        self.key, self.F, self.det, self.last_t = key, Features(), STRATEGIES[key](cfg or {}), None

    def feed(self, done):
        sig = None
        for c in done:
            t = c.get("t")
            if t is None or (self.last_t is not None and t <= self.last_t):
                continue
            self.F.update(dict(t=t, o=c["o"], h=c["h"], l=c["l"], c=c["c"], v=c.get("v")))
            sig = self.det.update(self.F)
            self.last_t = t
        return sig

    def status(self):
        """Compact, JSON-safe view for the dashboard: regime, setups in progress, recent setup decisions."""
        reg, detail = classify(self.F) if self.F.ready() else ("WARMING UP", f"{self.F.i + 1} candles")
        keep = ("side", "kind", "level", "level_kind", "status", "reason", "entry", "invalidation", "objective")
        fmt = lambda s: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in s.items() if k in keep}
        return dict(strategy=LABELS[self.key], experimental=True, regime=reg, regime_detail=detail,
                    vwap=round(self.F.vwap, 1) if self.F.vwap else None,
                    rvol=round(self.F.rvol, 2) if self.F.rvol else None,
                    active=[fmt(s) for s in self.det.active],
                    recent=[dict(fmt(s), t=str(s.get("t_confirm") or s.get("t"))[11:16]) for s in self.det.log[-8:]][::-1])
