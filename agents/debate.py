"""
Researcher / debate agent.

Puts the analysts' reports through a bull-vs-bear debate before the trader decides:
  * The bull researcher collects every bullish point; the bear researcher every bearish one.
    Each point counts by its own weight times how much we trust that analyst.
  * Both cases are scored 0..1; margin = bull − bear (−1 … +1).
  * When a trade is proposed, the opposing case is put to it as counter-arguments, and two
    bias checks run: "only the chart agrees" (the strategy's own indicators are the sole
    support) and "event day" (a big scheduled event makes any read less reliable).

It's rule-based and deterministic, so the same inputs always give the same verdict.
"""
from . import clip

# how much each analyst counts in the debate (sums to 1)
TRUST = {"technical": 0.27, "news": 0.22, "global": 0.15, "crude": 0.15, "sentiment": 0.13, "fundamental": 0.08}
NAMES = {"technical": "Technical", "news": "News", "global": "Global markets", "crude": "Crude",
         "sentiment": "Sentiment", "fundamental": "Fundamental"}


def _views(reports):
    """Normalise every analyst into (score, bull points, bear points)."""
    out = {}
    for k, r in reports.items():
        if not r or not r.get("ok") or k not in TRUST:
            continue
        if k == "crude":
            ch = r.get("change_pct") or 0
            s = clip(-ch / 2)
            pt = [(f"{r.get('name', 'Crude')} {ch:+.2f}% — " + ("falling oil helps India" if ch < 0 else "rising oil hurts India"),
                   abs(s))]
            out[k] = (s, pt if s > 0 else [], pt if s < 0 else [])
        elif k == "news":
            s = r.get("score", 0)
            pt = [(f"News sentiment {s:+.2f} over {r.get('headline_count', 0)} headlines", abs(s))]
            out[k] = (s, pt if s > 0 else [], pt if s < 0 else [])
        else:
            out[k] = (r.get("score", 0), [tuple(p) for p in r.get("bull", [])], [tuple(p) for p in r.get("bear", [])])
    return out


def run(reports, side=None):
    views = _views(reports)
    cover = sum(TRUST[k] for k in views) or 1.0
    bull = sum(TRUST[k] * max(s, 0) for k, (s, _, _) in views.items()) / cover
    bear = sum(TRUST[k] * max(-s, 0) for k, (s, _, _) in views.items()) / cover
    margin = round(bull - bear, 2)

    def case(idx):
        pts = [(f"{NAMES[k]}: {t}", w * TRUST[k]) for k, v in views.items() for t, w in v[idx]]
        return [[t, round(w, 3)] for t, w in sorted(pts, key=lambda p: -p[1])]

    bull_case, bear_case = case(1), case(2)
    verdict = "bulls win" if margin >= 0.15 else "bears win" if margin <= -0.15 else "balanced"

    flags = []
    news = reports.get("news") or {}
    fund = reports.get("fundamental") or {}
    events = list(news.get("events") or []) + [f"{s} results" for s in fund.get("results_today") or []]
    events = [e for e in events if e != "Expiry day"]
    if events:
        flags.append("Event day (" + ", ".join(events[:3]) + ") — reads are less reliable, expect whipsaws")
    challenges = []
    if side in ("CE", "PE"):
        want = 1 if side == "CE" else -1
        supporters = [k for k, (s, _, _) in views.items() if s * want > 0.1]
        opposers = [k for k, (s, _, _) in views.items() if s * want < -0.1]
        if supporters == ["technical"] and len(opposers) >= 2:
            flags.append("Only the chart supports this trade; " + ", ".join(NAMES[k] for k in opposers) + " disagree")
        challenges = (bear_case if side == "CE" else bull_case)[:4]
    if len(views) < 3:
        flags.append(f"Only {len(views)} analyst(s) reporting — thin evidence")

    return dict(bull_strength=round(bull, 2), bear_strength=round(bear, 2), margin=margin, verdict=verdict,
                bull_case=bull_case[:6], bear_case=bear_case[:6], challenges=challenges, flags=flags,
                analysts={k: round(s, 2) for k, (s, _, _) in views.items()}, coverage=round(cover, 2), side=side)
