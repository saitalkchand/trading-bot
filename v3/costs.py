"""
Trading-cost scenarios (Indian index options, Zerodha-style). Rates per side unless noted.

slippage   : fraction of premium lost to fill quality on each order
half_spread: half the bid/ask spread, paid on each order
delay_bars : 0 = fill at the signal bar's close; 1 = fill at the next bar's open (execution delay)
"""
SCENARIOS = {
    # what backtest.py (V2) assumes: charges only, perfect fills — used to verify the engine reproduces V2
    "v2_model":     dict(brokerage=20, stt_sell=0.0015, txn=0.0003553, sebi=1e-6, stamp_buy=0.00003, gst=0.18,
                         slippage=0.0, half_spread=0.0, delay_bars=0),
    "optimistic":   dict(brokerage=20, stt_sell=0.0015, txn=0.0003553, sebi=1e-6, stamp_buy=0.00003, gst=0.18,
                         slippage=0.001, half_spread=0.001, delay_bars=0),
    "base":         dict(brokerage=20, stt_sell=0.0015, txn=0.0003553, sebi=1e-6, stamp_buy=0.00003, gst=0.18,
                         slippage=0.0025, half_spread=0.0025, delay_bars=0),
    "conservative": dict(brokerage=20, stt_sell=0.0015, txn=0.0003553, sebi=1e-6, stamp_buy=0.00003, gst=0.18,
                         slippage=0.005, half_spread=0.004, delay_bars=1),
    "stress":       dict(brokerage=20, stt_sell=0.0015, txn=0.0003553, sebi=1e-6, stamp_buy=0.00003, gst=0.18,
                         slippage=0.01, half_spread=0.0075, delay_bars=1),
}


def fill(price, side, sc):
    """Model fill: buys pay up, sells receive less."""
    adj = sc["slippage"] + sc["half_spread"]
    return price * (1 + adj) if side == "buy" else max(0.05, price * (1 - adj))


def charges(buy_val, sell_val, sc, orders=2):
    brk = sc["brokerage"] * orders
    stt = sc["stt_sell"] * sell_val
    txn = sc["txn"] * (buy_val + sell_val)
    sebi = sc["sebi"] * (buy_val + sell_val)
    stamp = sc["stamp_buy"] * buy_val
    return brk + stt + txn + sebi + stamp + sc["gst"] * (brk + txn + sebi)
