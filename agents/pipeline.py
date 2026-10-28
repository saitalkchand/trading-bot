"""
The agent team, in order:

  analysts (fundamental, sentiment, news, technical, crude, global markets)   — each writes a report
      → debate (bull vs bear researchers)                       — challenges the reads
      → trader (BUY CE / BUY PE / HOLD)                         — needs a strategy signal
      → risk manager (hard checks and vetoes)                   — final approval

Analysts refresh in the background (bot.py thread, or the panel's Refresh button) and
cache to agents.json; the decision steps run on the cached reports when a signal fires.
"""
import time

from . import crude, debate, fundamental, global_markets, load_cache, news, save, sentiment, technical, trader

# key, module, on/off setting, seconds between refreshes
ANALYSTS = [
    ("crude", crude, "USE_CRUDE_AGENT", 170),
    ("global", global_markets, "USE_GLOBAL_AGENT", 170),
    ("technical", technical, "USE_TECHNICAL_AGENT", 290),
    ("news", news, "USE_NEWS_AGENT", 590),
    ("sentiment", sentiment, "USE_SENTIMENT_AGENT", 890),
    ("fundamental", fundamental, "USE_FUNDAMENTAL_AGENT", 12 * 3600),
]


def _g(cfg):
    return (lambda k: cfg.get(k)) if isinstance(cfg, dict) else (lambda k: getattr(cfg, k, None))


def refresh(cfg, force=False):
    """Refresh every analyst that is switched on. Returns a list of error strings."""
    g, errs = _g(cfg), []
    for key, mod, flag, age in ANALYSTS:
        if not g(flag):
            continue
        # a forced refresh still leaves fundamentals alone for an hour (12 slow lookups)
        max_age = (3600 if key == "fundamental" else 0) if force else age
        try:
            if key == "crude":
                mod.get(g("CRUDE_BLOCK_PCT"), max_age=max_age)
            elif key == "news":
                mod.get(g("NEWS_BLOCK_SCORE"), max_age=max_age)
            else:
                mod.get(max_age=max_age)
        except Exception as e:
            errs.append(f"{key}: {e!r}"[:150])
    return errs


def reports(cfg):
    g, c = _g(cfg), load_cache()
    return {key: c.get(key) for key, _, flag, _ in ANALYSTS if g(flag)}


def stance(cfg):
    """Debate and trader view on the current reports, without a strategy signal (for the panel)."""
    deb = debate.run(reports(cfg))
    return deb, trader.decide(None, deb, cfg)


def record(index, sig, deb, dec, risk, outcome):
    """Keep the last few decisions so the panel can show the full chain."""
    entry = dict(time=time.strftime("%H:%M:%S"), date=time.strftime("%Y-%m-%d"), index=index, side=sig["side"],
                 why=sig["why"], debate=dict(margin=deb["margin"], verdict=deb["verdict"], flags=deb["flags"],
                                             challenges=deb["challenges"][:3]),
                 trader=dec, risk=risk, outcome=outcome)
    hist = (load_cache().get("decisions") or {}).get("items", [])
    save("decisions", dict(items=([entry] + hist)[:20]))
    return entry
