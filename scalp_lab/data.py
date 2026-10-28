"""Frozen datasets. Experiments always name the snapshot they ran on, so results stay comparable even after
Yahoo's rolling ~30-day 1-min window moves on (scalp_backtest.py's cache is overwritten daily)."""
import hashlib
import os
import pickle

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT = "snapshot_2026-09-16_2026-10-14.pkl"


def load(name=DEFAULT):
    path = os.path.join(HERE, name)
    with open(path, "rb") as f:
        data = pickle.load(f)
    return data, dict(name=name, sha256=hashlib.sha256(open(path, "rb").read()).hexdigest()[:16])


def snapshot_from_cache(cache_path):
    """Freeze scalp_backtest.py's current cache under its date range (python -m scalp_lab.data)."""
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
    idx = data["idx"]["NIFTY"].index
    name = f"snapshot_{idx[0].date()}_{idx[-1].date()}.pkl"
    with open(os.path.join(HERE, name), "wb") as f:
        pickle.dump(data, f)
    return name


if __name__ == "__main__":
    import scalp_backtest
    scalp_backtest.load()
    print("saved", snapshot_from_cache(scalp_backtest.CACHE))
