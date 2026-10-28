"""Risk-based sizing: never more than the configured lots; fewer (or none) when the money at risk doesn't fit."""


def lots_for_risk(stop_pts, cost_unit, lot_size, configured_lots, budget):
    """Lots whose loss at the stop (premium points + round-trip costs per unit) fits within budget rupees.
    Returns (lots, reason). lots is min(configured, affordable); 0 means reject — it never scales UP."""
    per_lot = lot_size * (stop_pts + cost_unit)
    if per_lot <= 0:
        return 0, "invalid stop"
    affordable = int(budget // per_lot)
    if affordable < 1:
        return 0, f"one lot risks ₹{per_lot:,.0f} > ₹{max(budget, 0):,.0f} allowed"
    lots = min(configured_lots, affordable)
    return lots, "" if lots == configured_lots else f"reduced to {lots} lot(s): ₹{per_lot:,.0f} risk per lot"
