"""
Strategy V3 — the changes vs V2 that survived testing (evidence: reports/v3/V3_REPORT.md).
Everything not listed here stays exactly as in V2. Research only: the live bot is unchanged.
"""
# V2 + the one entry filter that held up
V3_FILTER_ONLY = dict(
    ENABLE_TIME_FILTER=True, TIME_BLOCKS=[(0, 601)],      # no new entries with signal close before 10:00
)

V3 = dict(
    V3_FILTER_ONLY,
    ENABLE_RISK_BUDGET=True,       # size each trade so its loss at the stop (premium model + costs)
    MAX_OPEN_RISK=6000,            # ... is at most ₹6,000 (2% of a ₹3L account) and never more than
    MAX_DAILY_LOSS=12000,          # ... what's left of the ₹12,000 daily loss limit (enforced BEFORE entry)
    LOTS=4,                        # upper bound on size
)
