"""Market agents: crude oil (agents.crude) and news sentiment (agents.news).
Both cache their latest result in agents.json so the bot and the web panel share it."""
import json, os, time

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents.json")


def load_cache():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(key, data):
    c = load_cache()
    data["fetched_at"] = time.time()
    c[key] = data
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CACHE)
    return data


def cached(key, max_age):
    d = load_cache().get(key)
    if d and time.time() - d.get("fetched_at", 0) < max_age:
        return d
    return None


# ---- shared report shape for the analyst agents ----------------------------------
# Every analyst returns: ok, score (-1 bearish … +1 bullish for Indian indices), label,
# bull / bear: lists of [point, weight], note. The debate agent reads only these fields.

def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def label(score, strong=0.4):
    return ("bullish" if score >= strong else "bearish" if score <= -strong
            else "mildly bullish" if score > 0.1 else "mildly bearish" if score < -0.1 else "neutral")


def report(points, note="", **extra):
    """points: list of (text, signed weight). Score = sum of weights, clipped to ±1."""
    score = clip(sum(w for _, w in points))
    return dict(ok=True, score=round(score, 2), label=label(score),
                bull=[[t, round(w, 2)] for t, w in points if w > 0],
                bear=[[t, round(-w, 2)] for t, w in points if w < 0],
                note=note, **extra)
