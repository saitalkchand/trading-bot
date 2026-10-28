# ============================================================
#  BOT SETTINGS — defaults. Values saved from the web app (Settings page)
#  go to settings.json and override these; you normally never edit this file.
# ============================================================
import json, os

MODE = "paper"                    # "paper" (simulated orders) or "live" (real money)
PAPER_FEED = "free"               # paper prices: "free" (no keys; Yahoo index + modelled options)
                                  #               "zerodha" (real option prices; needs API key + login)

# ---- Kite Connect credentials (stored only on this computer) ----
API_KEY = "jq7wtgsmp9tj7uju"
API_SECRET = "hm68ui22rwwk2th2r1ufnbtn5pjbvbsw"

# ---- What to trade ----
TRADE_INSTRUMENTS = "NIFTY"       # "NIFTY", "SENSEX" or "BOTH"
LOTS = 1                          # lots per trade (Nifty lot 65, Sensex lot 20 — read live from Kite)
ITM_STRIKES = 1                   # strikes in-the-money (1 = one strike ITM, 2 = two strikes ITM)
MAX_OPEN_POSITIONS = 1            # Nifty & Sensex move together — 1 avoids doubling the same bet
CANDLE_MINUTES = 5

# ---- Strategy v2 (see strategy.py) ----
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50                    # trade only in the direction of this trend EMA
CROSS_LOOKBACK = 3                # EMA cross must be within the last N candles
RSI_PERIOD = 14
RSI_BUY_CE = 55                   # CE needs RSI >= this ...
RSI_BUY_PE = 45                   # PE needs RSI <= this ...
RSI_MAX = 75                      # ... but skip exhausted moves (CE RSI > 75, PE RSI < 25)
MIN_ATR_PCT = 0.05                # skip when 5-min ATR < 0.05% of index (dead market)

# ---- Risk : reward (on the INDEX level) ----
RR = 2.0                          # target = 2 x risk  -> 1:2
BREAKEVEN_AT_R = 1.0              # at 1:1, move stop to COST: entry + charges & slippage (0 = off)
COST_STOP_ON_PREMIUM = False      # also exit if the OPTION falls back to cost (stricter; tested worse)
TRAIL_START_R = 0                 # from +R x this, trail the stop (0 = off; tested: mixed results)
TRAIL_R = 1.0                     # ... this many R behind the best price (0 = off)
TIME_STOP_MIN = 0                 # exit if not +0.5R within N minutes (0 = off)

VIX_ADJUST_EXITS = True           # scale stop & target by sqrt(India VIX / VIX_BASE): wider when fearful, tighter when calm
VIX_BASE = 14.0                   # "normal" India VIX: at this level stops are unchanged
VIX_MULT_MIN = 0.85               # never tighter than 0.85 x the ATR/swing stop
VIX_MULT_MAX = 1.3                # never wider than 1.3 x

# ---- Entry filters ----
HTF_FILTER = True                 # 15-min EMA-20 must agree with the trade (tested: helps a little)
ADX_MIN = 0                       # skip when ADX(14) is below this (0 = off; 20 = fewer, choppy-free trades)
VOLUME_FILTER = True              # signal candle needs above-normal volume (indices have none: VOLUME_SYMBOL is the proxy)
VOLUME_SYMBOL = "NIFTYBEES.NS"    # Nifty ETF; used for Sensex too (same big stocks)
VOLUME_MIN_RVOL = 1.2             # candle volume per minute >= this x the average of the previous VOLUME_AVG_N candles
VOLUME_AVG_N = 20

# ---- Agents ----
USE_CRUDE_AGENT = True
CRUDE_BLOCK_PCT = 1.0             # Brent up >= 1% blocks CE; down >= 1% blocks PE
USE_NEWS_AGENT = True
NEWS_BLOCK_SCORE = 0.4            # sentiment <= -0.4 blocks CE; >= +0.4 blocks PE
NEWS_BLOCK_ON_EVENT = False       # True = no new trades on RBI/Fed/Budget/result days
USE_GLOBAL_AGENT = True           # GIFT Nifty, US / Europe / Asia indices, US futures, dollar, rupee, US yields
GLOBAL_BLOCK_SCORE = 0.6          # global score <= -0.6 blocks CE; >= +0.6 blocks PE (only strong worldwide moves)
USE_TECHNICAL_AGENT = True        # RSI / MACD / Bollinger read, feeds the debate
USE_SENTIMENT_AGENT = True        # Reddit chatter, India VIX, heavyweight breadth
USE_FUNDAMENTAL_AGENT = True      # earnings health of Nifty heavyweights (refreshes every 12 h)
USE_TRADER_AGENT = True           # debate + trader may overrule a strategy signal (HOLD)
TRADER_BLOCK_MARGIN = 0.35        # HOLD when the debate is this far against the signal (0-1)
AGENT_AUTO_REFRESH = True         # control panel keeps agents fresh even while the bot is stopped

# ---- Scalper (scalper.py): same entry rules + agents + ITM options, on fast candles, fixed point exits ----
SCALP_STRATEGY = "v2"             # "v2" = Scalper V2 (scalp_v2.py), "ema" = main bot's EMA-cross rules,
                                  # "volume" = volume-spike breakout. V2 did NOT pass its backtest either — paper only.
SCALP_VOL_SYMBOL = "NIFTYBEES.NS" # volume proxy (indices have no volume); used for Sensex too
SCALP_VOL_MULT = 1.5              # volume must be this many times its recent average
SCALP_VOL_AVG_N = 20              # candles in that average
SCALP_VOL_BREAKOUT_N = 5          # close must break the high/low of this many previous candles
SCALP_POINTS_ON = "premium"      # "premium" = points on the option price, "index" = points on the index
SCALP_SL_POINTS = 15              # stop-loss (option premium points)
SCALP_TARGET_POINTS = 30          # target (1:2)
SCALP_BE_AT_POINTS = 15           # at +15 (1R) move the stop to cost (entry + charges); 0 = off
SCALP_LOTS = 1
SCALP_CANDLE_MINUTES = 1          # signal candles (the main bot uses 5)
SCALP_MIN_ATR_PCT = 0.02          # 1-min candles move less, so the dead-market filter is lower
SCALP_MAX_HOLD_MIN = 30           # exit a scalp that has gone nowhere after 30 minutes (0 = off)
SCALP_COOLDOWN_MIN = 5            # wait after each exit before a new entry (V2 also needs a NEW setup after a stop)
SCALP_MAX_TRADES_PER_DAY = 6
SCALP_MAX_DAILY_LOSS = 2500       # checked BEFORE each entry against the trade's loss at the stop (≈2 x 15-pt stops)
SCALP_MAX_CONSECUTIVE_LOSSES = 2  # 2 losses in a row = done for the day; no agent can override
SCALP_NO_ENTRY_BEFORE = "09:35"
SCALP_NO_ENTRY_AFTER = "14:30"
SCALP_AVOID_OVERLAP = True        # no scalp on an index while the main bot holds a position there
# ---- Scalper V2 (scalp_v2.py; tested by scalp_v2_backtest.py → reports/scalper_v2/) ----
SCALP_V2_STRICT = False           # True = every V2 rule mandatory (backtest: ~1 trade in 18 days)
SCALP_REGIME_FILTER = True        # trade only STRONG up/down trends (weak ones need a very high score)
SCALP_ADX_MIN = 20
SCALP_ATR_MIN_PCT = 0.025         # 1-min ATR as % of the index; below = dead market
SCALP_EMA_SEPARATION_MIN = 0.03   # |EMA9 - EMA21| as % of price
SCALP_CHOP_FILTER = True          # choppiness index > 61.8 or >= 3 EMA crosses in 20 candles = RANGE
SCALP_ALLOW_WEAK_TREND = True
SCALP_WEAK_TREND_SCORE_MIN = 85
SCALP_EMA_SLOPE_LOOKBACK = 3
SCALP_PULLBACK_ENTRY = True       # pullback to EMA9/21 that holds EMA21, then a break of the pullback high/low
SCALP_PULLBACK_LOOKBACK = 3
SCALP_BREAKOUT_ENTRY = True       # volume breakout, entered on the NEXT candle if it holds (false-breakout guard)
SCALP_BREAKOUT_CONFIRM = True
SCALP_BREAKOUT_LOOKBACK = 5
SCALP_MAX_VWAP_DISTANCE_PCT = 0.12
SCALP_CE_RSI_MIN, SCALP_CE_RSI_MAX = 52, 68
SCALP_PE_RSI_MIN, SCALP_PE_RSI_MAX = 32, 48
SCALP_USE_MACD = True
SCALP_MIN_BODY_PCT = 0.03
SCALP_MAX_OPPOSITE_WICK_RATIO = 0.40
SCALP_MAX_SIGNAL_ATR = 1.5        # skip a signal candle bigger than 1.5 x ATR (chasing)
SCALP_OPENING_RANGE_FILTER = True
SCALP_MAX_OPENING_RANGE_PCT = 0.35
SCALP_CROSS_INDEX_FILTER = True   # Nifty checks Bank Nifty, Sensex checks Nifty: skip if strongly opposite
SCALP_VIX_FILTER = True           # skip bottom-10% VIX with low ATR, and VIX above its 95th percentile
TRADE_SCORE_MIN = 75
SCALP_CE_ENABLED = True
SCALP_PE_ENABLED = True
SCALP_SIDE_ADAPT = True           # a side whose last 8 closed scalps have PF < 0.8 needs +10 score
SCALP_MAX_SPREAD_PCT = 1.0        # option bid/ask spread limit (live quotes only)
SCALP_MIN_OPTION_VOLUME = 0       # absolute floors (0 = off); relative check below always applies
SCALP_MIN_OPTION_OI = 0
SCALP_MIN_REL_LIQUIDITY = 0.25    # option volume/OI must be >= 25% of the median of nearby strikes
SCALP_DELTA_MIN = 0.55            # preferred |delta| band for the traded option
SCALP_DELTA_MAX = 0.70
SCALP_DYNAMIC_SL = False          # True = premium stop from 1-min ATR x delta x 0.8 (5–12 pts); False = SCALP_SL_POINTS
SCALP_MIN_SL_POINTS = 5
SCALP_MAX_SL_POINTS = 12
SCALP_ATR_SL_MULT = 0.8
SCALP_RR = 2.0                    # V2 target = 2 x the actual stop
SCALP_BE_AT_R = 1.0               # V2: stop to cost after 1 x risk in profit (0 = off); 1.0/1.2/1.5 tested the same
SCALP_STRUCTURAL_COOLDOWN = True  # after a stop, same side needs a reset through EMA9 and a fresh setup
SCALP_ALLOW_LIVE = False          # the scalper refuses live mode unless this is switched on (backtest: loses with real fills)

# ---- HARD RISK LIMITS ----
MAX_DAILY_LOSS = 3000
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_LOSSES = 2
MAX_PREMIUM_PER_TRADE = 40000     # ITM options cost more — total premium cap per trade

# ---- Timing (IST) ----
NO_ENTRY_BEFORE = "09:30"
NO_ENTRY_AFTER = "13:30"          # afternoons were where v1 bled (square-off losses)
SQUARE_OFF_AT = "15:10"

POLL_SECONDS = 5

# ---- Instrument specs (lot size & expiry are read live from Kite; these are backtest fallbacks) ----
INSTRUMENT_SPECS = {
    "NIFTY":  dict(index="NSE:NIFTY 50", token=256265, exchange="NFO", name="NIFTY",  step=50,  lot=65, yahoo="^NSEI",  expiry_weekday=1),
    "SENSEX": dict(index="BSE:SENSEX",   token=265,    exchange="BFO", name="SENSEX", step=100, lot=20, yahoo="^BSESN", expiry_weekday=3),
}

# ---- apply settings saved from the web app ----
_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
if os.path.exists(_SETTINGS):
    with open(_SETTINGS) as _f:
        for _k, _v in json.load(_f).items():
            if _k.isupper() and _k != "INSTRUMENT_SPECS":
                globals()[_k] = _v
# ---- unsafe timings are corrected here so the bot, backtest and Settings page all agree
def _mins(v):
    h, m = map(int, str(v).split(":"))
    return h * 60 + m


TIME_WARNINGS = []
if _mins(SQUARE_OFF_AT) > _mins("15:20"):
    TIME_WARNINGS.append(f"Square-off {SQUARE_OFF_AT} is too late (Zerodha auto-squares F&O at 15:26 for a fee, market shuts 15:30) "
                         "— using 15:20. Save Settings to fix it.")
    SQUARE_OFF_AT = "15:20"
if _mins(NO_ENTRY_AFTER) >= _mins(SQUARE_OFF_AT):
    TIME_WARNINGS.append(f"No-entry time {NO_ENTRY_AFTER} is at/after square-off — using 13:30. Save Settings to fix it.")
    NO_ENTRY_AFTER = "13:30"

# old v1 settings that no longer apply
for _old in ("STOP_LOSS_PCT", "TARGET_PCT"):
    globals().pop(_old, None)


def instruments():
    if TRADE_INSTRUMENTS == "BOTH":
        return ["NIFTY", "SENSEX"]
    return [TRADE_INSTRUMENTS] if TRADE_INSTRUMENTS in INSTRUMENT_SPECS else ["NIFTY"]
