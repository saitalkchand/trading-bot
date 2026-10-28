"""
Kite Bot control panel — runs only on this computer (http://127.0.0.1:8765).

    python app.py        (start.command does this for you)
"""
import csv, json, os, signal, subprocess, sys, threading, time, webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
IST = ZoneInfo("Asia/Kolkata")
PORT = int(os.environ.get("KITEBOT_PORT", 8765))
SETTINGS = "settings.json"
TOKEN_META = "token_meta.json"

app = Flask(__name__, static_folder=None)
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.before_request
def only_this_computer():
    """Any web page open in your browser can send requests to 127.0.0.1. Refuse anything that
    isn't this control panel: wrong Host (DNS rebinding), a foreign Origin, or a POST that isn't
    JSON (a plain-form/text POST skips the browser's CORS check)."""
    if request.host not in LOCAL_HOSTS:
        return jsonify(ok=False, error="forbidden host"), 403
    origin = request.headers.get("Origin")
    if origin and origin.replace("http://", "", 1) not in LOCAL_HOSTS:
        return jsonify(ok=False, error="forbidden origin"), 403
    if request.method == "POST" and not request.is_json:
        return jsonify(ok=False, error="JSON body required"), 415
bot_proc = None
scalp_proc = None
sbt = {"running": False, "output": "", "started": None, "finished": None}
bt = {"running": False, "output": "", "started": None, "finished": None}

# ------------------------------------------------------------------ settings schema
# Sections appear on the Settings page in this order.
SECTIONS = [
    ("account", "Account & price feed", "Where prices come from, and your Zerodha app keys. Paper trading on the free feed needs no keys."),
    ("trade", "What to trade", "Which index the main bot trades, how big, and how deep in the money."),
    ("risk", "Risk limits", "Hard limits for the main bot. When one is hit, it stops trading for the rest of the day."),
    ("timing", "Trading hours", "When the main bot may open trades, and when it closes everything. India time."),
    ("exits", "Stop-loss & target", "How the main bot gets out of a trade. Stops and targets are measured on the index."),
    ("entry", "Entry rules", "When the main bot gets into a trade. The defaults are the tested values; change them only with a reason."),
    ("agents", "Agent team", "Market analysts that can block a trade. They never start one."),
    ("scalper", "Scalper", "The separate 1-minute scalping bot (Scalper page). It has its own limits."),
]


def F(key, section, typ, label, help="", unit="", lo=None, hi=None, adv=False, choices=None, dep=None, danger=False):
    return dict(key=key, section=section, type=typ, label=label, help=help, unit=unit, min=lo, max=hi,
                advanced=adv, choices=choices, dep=dep, danger=danger)


SCHEMA = [
    F("PAPER_FEED", "account", "select", "Paper trading prices",
      "Free uses Yahoo index prices (about a minute behind) and estimated option prices. Zerodha uses real option prices and needs a daily login.",
      choices=[("free", "Free — no keys needed"), ("zerodha", "Zerodha — real option prices")]),
    F("API_KEY", "account", "text", "Zerodha API key", "From your app at developers.kite.trade. Needed for live trading or Zerodha prices."),
    F("API_SECRET", "account", "secret", "Zerodha API secret", "Stored only on this Mac. Leave blank to keep the saved one."),

    F("TRADE_INSTRUMENTS", "trade", "select", "Index", "Nifty and Sensex move together, so trading both mostly doubles the same bet.",
      choices=[("NIFTY", "Nifty 50"), ("SENSEX", "Sensex"), ("BOTH", "Both (one position at a time)")]),
    F("LOTS", "trade", "int", "Lots per trade", "Nifty lot = 65 units, Sensex lot = 20. Start with 1 until paper results are steady.", "lots", 1, 50),
    F("ITM_STRIKES", "trade", "int", "Strikes in the money",
      "1 = at least one full strike in the money (Nifty ≥ 50 points, Sensex ≥ 100). Deeper costs more and moves more like the index — both ways.", "strikes", 1, 3),
    F("MAX_OPEN_POSITIONS", "trade", "int", "Open positions at once", "Keep at 1 when trading both indices.", "", 1, 2, adv=True),

    F("MAX_DAILY_LOSS", "risk", "float", "Max loss per day", "Stop for the day once losses reach this.", "₹", 0),
    F("MAX_TRADES_PER_DAY", "risk", "int", "Max trades per day", "Fewer, better trades usually beat more trades.", "trades", 1, 20),
    F("MAX_CONSECUTIVE_LOSSES", "risk", "int", "Stop after losses in a row", "Stop for the day after this many losing trades in a row.", "losses", 1, 10),
    F("MAX_PREMIUM_PER_TRADE", "risk", "float", "Max premium per trade", "Caps the money spent buying options in one trade. In-the-money options cost more.", "₹", 0),

    F("NO_ENTRY_BEFORE", "timing", "time", "First entry at", "Skips the noisy first 15 minutes after the 09:15 open."),
    F("NO_ENTRY_AFTER", "timing", "time", "Last entry at", "In testing, late entries were where most losses came from. 13:30 is the tested value."),
    F("SQUARE_OFF_AT", "timing", "time", "Close everything at", "15:20 at the latest. Zerodha auto-closes F&O intraday positions at 15:26 and charges ₹50 + GST per order."),

    F("RR", "exits", "float", "Target", "Target distance as a multiple of the stop distance. 2 means risk 1 to make 2.", "× stop", 0.5, 5),
    F("BREAKEVEN_AT_R", "exits", "float", "Move stop to cost at",
      "Once the trade gains this many times its risk (1 = 1:1), the stop moves to your entry plus charges, so it can't turn into a loss. 0 = off.", "× stop", 0, 5),
    F("COST_STOP_ON_PREMIUM", "exits", "bool", "Cost stop on option price too",
      "Also exit if the option price falls back to cost. Stricter; it cut profit in testing because time decay triggers it early.", adv=True),
    F("TRAIL_START_R", "exits", "float", "Start trailing stop at", "After this profit, the stop follows the best price. 0 = off (mixed results in testing).", "× stop", 0, 5, adv=True),
    F("TRAIL_R", "exits", "float", "Trailing distance", "How far behind the best price the trailing stop sits. 0 = off.", "× stop", 0, 5, adv=True),
    F("VIX_ADJUST_EXITS", "exits", "bool", "Scale stop & target with India VIX",
      "Stop and target are multiplied by √(VIX ÷ normal VIX): wider when the market is fearful, tighter when calm. Target stays the same multiple of the stop."),
    F("VIX_BASE", "exits", "float", "Normal India VIX", "At this VIX the stop is unchanged.", "", 8, 40, dep="VIX_ADJUST_EXITS", adv=True),
    F("VIX_MULT_MIN", "exits", "float", "Tightest VIX scaling", "", "×", 0.5, 1, dep="VIX_ADJUST_EXITS", adv=True),
    F("VIX_MULT_MAX", "exits", "float", "Widest VIX scaling", "A wider stop also means a bigger loss when it's hit; the risk manager checks it against today's loss budget.", "×", 1, 2, dep="VIX_ADJUST_EXITS", adv=True),
    F("TIME_STOP_MIN", "exits", "float", "Time stop", "Exit a trade that hasn't reached half its risk in profit after this long. 0 = off.", "min", 0, 360, adv=True),

    F("HTF_FILTER", "entry", "bool", "15-minute trend must agree", "Only trade when the 15-minute trend points the same way. Helped in testing."),
    F("ADX_MIN", "entry", "float", "Minimum trend strength (ADX)", "Skip choppy markets. 0 = off. 20 means fewer trades and smaller drawdowns, but lower profit in testing.", "", 0, 50),
    F("VOLUME_FILTER", "entry", "bool", "Volume must confirm the signal",
      "Indices have no volume, so the Nifty ETF (NIFTYBEES) is used: the signal candle must trade more than usual. If volume data is missing, the check is skipped."),
    F("VOLUME_MIN_RVOL", "entry", "float", "Volume needed", "Signal candle volume as a multiple of the average of the previous candles.", "× normal", 0.5, 5, dep="VOLUME_FILTER"),
    F("VOLUME_AVG_N", "entry", "int", "Volume average over", "", "candles", 5, 100, dep="VOLUME_FILTER", adv=True),
    F("MIN_ATR_PCT", "entry", "float", "Minimum volatility", "Skip dead markets where the 5-minute range is below this share of the index.", "%", 0, 1),
    F("EMA_FAST", "entry", "int", "Fast average", "Entry signal: the fast average crossing the slow one.", "candles", 2, 50, adv=True),
    F("EMA_SLOW", "entry", "int", "Slow average", "", "candles", 3, 100, adv=True),
    F("EMA_TREND", "entry", "int", "Trend average", "Trades only in the direction of this longer average.", "candles", 10, 200, adv=True),
    F("RSI_BUY_CE", "entry", "float", "Calls need RSI at least", "Momentum check for bullish trades.", "", 50, 80, adv=True),
    F("RSI_BUY_PE", "entry", "float", "Puts need RSI at most", "Momentum check for bearish trades.", "", 20, 50, adv=True),
    F("RSI_MAX", "entry", "float", "Skip exhausted moves beyond RSI", "Skips calls above this RSI and puts below 100 minus it.", "", 60, 95, adv=True),

    F("USE_CRUDE_AGENT", "agents", "bool", "Crude-oil agent", "Blocks calls when Brent jumps and puts when it falls. Reduced drawdown in testing."),
    F("CRUDE_BLOCK_PCT", "agents", "float", "Crude move that blocks", "Brent up this much blocks calls; down this much blocks puts.", "%", 0.1, 10, dep="USE_CRUDE_AGENT"),
    F("USE_NEWS_AGENT", "agents", "bool", "News agent", "Scores the last 24 hours of Indian market headlines."),
    F("NEWS_BLOCK_SCORE", "agents", "float", "News score that blocks", "Sentiment runs from −1 to +1. Beyond this level, the opposite side is blocked.", "", 0, 1, dep="USE_NEWS_AGENT"),
    F("NEWS_BLOCK_ON_EVENT", "agents", "bool", "No trades on big-event days", "RBI policy, Fed decision, Budget and results days often whipsaw.", dep="USE_NEWS_AGENT"),
    F("USE_GLOBAL_AGENT", "agents", "bool", "Global markets agent",
      "GIFT Nifty, US, Europe and Asia indices, US futures, the dollar, the rupee and US yields, rolled into one score for the debate."),
    F("GLOBAL_BLOCK_SCORE", "agents", "float", "Global score that blocks",
      "From −1 to +1. A worldwide sell-off this strong blocks calls; a rally this strong blocks puts.", "", 0.1, 1, dep="USE_GLOBAL_AGENT"),
    F("USE_TECHNICAL_AGENT", "agents", "bool", "Technical analyst", "RSI, MACD and Bollinger read for the debate."),
    F("USE_SENTIMENT_AGENT", "agents", "bool", "Sentiment analyst", "Reddit chatter, India VIX and how many big stocks are up."),
    F("USE_FUNDAMENTAL_AGENT", "agents", "bool", "Fundamental analyst", "Earnings health of the 12 biggest Nifty stocks. Refreshes every 12 hours."),
    F("USE_TRADER_AGENT", "agents", "bool", "Let the debate overrule signals", "When the bull vs bear debate is strongly against a signal, the trader holds."),
    F("TRADER_BLOCK_MARGIN", "agents", "float", "How strongly against", "The debate scores bull minus bear from −1 to +1. At this much against a signal, the trader holds.", "", 0, 1, dep="USE_TRADER_AGENT"),
    F("AGENT_AUTO_REFRESH", "agents", "bool", "Auto-refresh while the bot is stopped",
      "Keeps agents current while this panel is open. Each refetches only when due (crude 3 min … fundamentals 12 h)."),

    F("SCALP_STRATEGY", "scalper", "select", "Entry rule",
      "EMA cross: the main bot's rules on fast candles. Volume spike: a breakout candle on 1.5× normal volume, on the VWAP side. Experimental rules are research from the Strategy lab page and refuse live mode. Every rule lost money in the backtest with realistic fills.",
      choices=[("v2", "Scalper V2 — filtered (recommended for paper)"), ("ema", "EMA cross (main bot rules)"), ("volume", "Volume spike breakout"),
               ("sweep", "EXPERIMENTAL · Liquidity sweep reversal (paper only)"),
               ("retest", "EXPERIMENTAL · Breakout-retest (paper only)")]),
    F("TRADE_SCORE_MIN", "scalper", "int", "V2: minimum trade score", "Trend 20, momentum 15, VWAP 10, RSI 10, MACD 10, volume 10, breakout 10, option liquidity 10, volatility 5. Higher = fewer, cleaner trades.", "/ 100", 50, 100),
    F("SCALP_CE_ENABLED", "scalper", "bool", "V2: take CE (bullish) scalps"),
    F("SCALP_PE_ENABLED", "scalper", "bool", "V2: take PE (bearish) scalps"),
    F("SCALP_DYNAMIC_SL", "scalper", "bool", "V2: stop sized to volatility", "On: premium stop = 1-min ATR × delta × 0.8 (5–12 pts), target = 2 × the stop. Off (current): V2 uses the fixed Stop-loss above and a 2× target — 15 / 30 points."),
    F("SCALP_BE_AT_R", "scalper", "float", "V2: move stop to cost at", "In multiples of the stop. 1.0, 1.2 and 1.5 gave the same backtest result. 0 = off.", "× stop", 0, 5),
    F("SCALP_MAX_SPREAD_PCT", "scalper", "float", "V2: max option spread", "Skip an option whose bid/ask spread is wider than this. Needs the Zerodha feed — the free feed has no order book.", "%", 0.1, 5),
    F("SCALP_V2_STRICT", "scalper", "bool", "V2: strict mode", "Every V2 rule mandatory (RSI, MACD, structure, body). The backtest found ~1 trade in 18 days.", adv=True),
    F("SCALP_REGIME_FILTER", "scalper", "bool", "V2: trend-regime filter", "Only strong up/downtrends trade; range, choppy and high-volatility minutes are skipped.", adv=True),
    F("SCALP_ADX_MIN", "scalper", "float", "V2: ADX for a strong trend", "", "", 10, 50, adv=True),
    F("SCALP_ATR_MIN_PCT", "scalper", "float", "V2: minimum 1-min ATR", "As a % of the index. Below this the market counts as dead.", "%", 0, 1, adv=True),
    F("SCALP_EMA_SEPARATION_MIN", "scalper", "float", "V2: minimum EMA9–EMA21 gap", "", "%", 0, 1, adv=True),
    F("SCALP_MAX_VWAP_DISTANCE_PCT", "scalper", "float", "V2: max distance from VWAP", "Don't chase: wait for a pullback when price is further than this from VWAP.", "%", 0.02, 1, adv=True),
    F("SCALP_CE_RSI_MIN", "scalper", "float", "V2: CE RSI from", "", "", 0, 100, adv=True),
    F("SCALP_CE_RSI_MAX", "scalper", "float", "V2: CE RSI up to", "", "", 0, 100, adv=True),
    F("SCALP_PE_RSI_MIN", "scalper", "float", "V2: PE RSI from", "", "", 0, 100, adv=True),
    F("SCALP_PE_RSI_MAX", "scalper", "float", "V2: PE RSI up to", "", "", 0, 100, adv=True),
    F("SCALP_USE_MACD", "scalper", "bool", "V2: MACD confirmation", adv=True),
    F("SCALP_PULLBACK_ENTRY", "scalper", "bool", "V2: pullback entries", "Pullback to EMA9/21 that holds EMA21, then a break of the pullback high/low.", adv=True),
    F("SCALP_BREAKOUT_ENTRY", "scalper", "bool", "V2: volume-breakout entries", "Entered one candle later, only if the breakout holds.", adv=True),
    F("SCALP_CROSS_INDEX_FILTER", "scalper", "bool", "V2: check the other index", "Nifty checks Bank Nifty, Sensex checks Nifty. Skips a trade the other index strongly disagrees with.", adv=True),
    F("SCALP_VIX_FILTER", "scalper", "bool", "V2: India VIX filter", "Skips the calmest 10% of days (with low ATR) and VIX spikes above the 95th percentile of the last 20 days.", adv=True),
    F("SCALP_OPENING_RANGE_FILTER", "scalper", "bool", "V2: opening-range filter", "If the first 5 minutes moved more than 0.35%, wait until volatility settles.", adv=True),
    F("SCALP_MIN_SL_POINTS", "scalper", "float", "V2: smallest premium stop", "", "pts", 1, 50, adv=True),
    F("SCALP_MAX_SL_POINTS", "scalper", "float", "V2: largest premium stop", "", "pts", 1, 100, adv=True),
    F("SCALP_DELTA_MIN", "scalper", "float", "V2: option delta from", "Preferred delta band when choosing the strike.", "", 0.3, 0.95, adv=True),
    F("SCALP_DELTA_MAX", "scalper", "float", "V2: option delta up to", "", "", 0.3, 0.95, adv=True),
    F("SCALP_STRUCTURAL_COOLDOWN", "scalper", "bool", "V2: fresh setup after a stop", "After a stop, the same side needs price to reset through EMA9 and a new setup, not just time.", adv=True),
    F("SCALP_SIDE_ADAPT", "scalper", "bool", "V2: tougher on a losing side", "If a side's last 8 closed scalps have profit factor under 0.8, it needs 10 more score points.", adv=True),
    F("SCALP_VOL_MULT", "scalper", "float", "Volume spike size", "Candle volume must be this many times its recent average. Indices have no volume, so NIFTYBEES volume is used.", "×", 1, 10, adv=True),
    F("SCALP_VOL_AVG_N", "scalper", "int", "Volume average over", "", "candles", 5, 100, adv=True),
    F("SCALP_VOL_BREAKOUT_N", "scalper", "int", "Breakout of last", "The close must clear the high (CE) or low (PE) of this many earlier candles.", "candles", 2, 30, adv=True),
    F("SCALP_POINTS_ON", "scalper", "select", "Points measured on", "Option price: 5/10 points on the premium. Index: 5/10 points on Nifty/Sensex.",
      choices=[("premium", "Option price"), ("index", "Index")]),
    F("SCALP_SL_POINTS", "scalper", "float", "Stop-loss", "On an ITM option (delta ≈ 0.6–0.7) 15 premium points is about 22 index points. Backtest: 5-point stops lost to noise and costs; 15/30 did better but on few trades.", "pts", 0.5, 200),
    F("SCALP_TARGET_POINTS", "scalper", "float", "Target", "", "pts", 0.5, 400),
    F("SCALP_BE_AT_POINTS", "scalper", "float", "Move stop to cost at", "After this many points in profit, the stop moves to entry plus charges. 0 = off.", "pts", 0, 200),
    F("SCALP_LOTS", "scalper", "int", "Lots per scalp", "", "lots", 1, 50),
    F("SCALP_MAX_DAILY_LOSS", "scalper", "float", "Max scalp loss per day", "Checked before every scalp against its loss at the stop.", "₹", 0),
    F("SCALP_MAX_TRADES_PER_DAY", "scalper", "int", "Max scalps per day", "", "scalps", 1, 50),
    F("SCALP_MAX_CONSECUTIVE_LOSSES", "scalper", "int", "Stop after losses in a row", "", "losses", 1, 10),
    F("SCALP_NO_ENTRY_BEFORE", "scalper", "time", "First scalp at"),
    F("SCALP_NO_ENTRY_AFTER", "scalper", "time", "Last scalp at", "Must be before the main square-off time."),
    F("SCALP_ALLOW_LIVE", "scalper", "bool", "Allow live scalping (real money)",
      "Off by default. The scalper backtest lost money with realistic fills. Paper-trade it first.", danger=True),
    F("SCALP_CANDLE_MINUTES", "scalper", "int", "Signal candles", "", "min", 1, 5, adv=True),
    F("SCALP_MAX_HOLD_MIN", "scalper", "float", "Time stop", "Exit a scalp that has gone nowhere after this long. 0 = off.", "min", 0, 120, adv=True),
    F("SCALP_COOLDOWN_MIN", "scalper", "float", "Wait after each exit", "", "min", 0, 60, adv=True),
    F("SCALP_AVOID_OVERLAP", "scalper", "bool", "Skip an index the main bot holds", "Avoids doubling up on the same move.", adv=True),
]
_PY = {"int": int, "float": float, "text": str, "secret": str, "time": str}
EDITABLE = {f["key"]: (tuple(c for c, _ in f["choices"]) if f["type"] == "select" else "bool" if f["type"] == "bool"
                       else _PY[f["type"]], f["section"], f["label"]) for f in SCHEMA}
LIMITS = {f["key"]: (f["min"], f["max"]) for f in SCHEMA}


# ------------------------------------------------------------------ helpers
def today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    """Fresh read of config.py + settings.json (no module caching)."""
    ns = {"__file__": os.path.join(BASE, "config.py")}
    with open("config.py") as f:
        exec(f.read(), ns)
    return {k: v for k, v in ns.items() if k.isupper()}


def default_config():
    """config.py values WITHOUT settings.json (what 'Reset to default' restores)."""
    ns = {"__file__": os.path.join(BASE, "__defaults__", "config.py")}
    with open("config.py") as f:
        exec(f.read(), ns)
    return {k: v for k, v in ns.items() if k.isupper()}


def kite_client(cfg):
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=cfg["API_KEY"])


def login_state():
    meta = read_json(TOKEN_META, {})
    ok = os.path.exists("access_token.txt") and meta.get("date") == today()
    return {"logged_in": ok, "user": meta.get("user") if ok else None,
            "login_time": meta.get("time") if ok else None}


def needs_login(cfg):
    return cfg["MODE"] == "live" or cfg.get("PAPER_FEED", "free") == "zerodha"


def pid_running(pidfile, script):
    """A bot left running by an earlier copy of this app still counts (its pid file says so),
    so Start can't launch a second copy that would double every order."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip() or 0)
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return script in out


def bot_running():
    global bot_proc
    if bot_proc is not None and bot_proc.poll() is None:
        return True
    bot_proc = None
    return pid_running("bot.pid", "bot.py")


def scalp_running():
    global scalp_proc
    if scalp_proc is not None and scalp_proc.poll() is None:
        return True
    scalp_proc = None
    return pid_running("scalp.pid", "scalper.py")


def read_trades(path="trades.csv"):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("qty", "entry", "exit", "pnl", "charges"):
            try:
                r[k] = float(r[k])
            except (ValueError, KeyError, TypeError):
                r[k] = 0.0
        try:  # older rows have no charges recorded: their net is the gross
            r["net_pnl"] = float(r["net_pnl"])
        except (ValueError, KeyError, TypeError):
            r["net_pnl"] = r["pnl"]
    return rows


def tail(path, n=200):
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        return "".join(f.readlines()[-n:])


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "web"), "index.html")


# ------------------------------------------------------------------ login
@app.get("/login")
def login():
    cfg = load_config()
    if not cfg.get("API_KEY"):
        return redirect("/#settings?need=key")
    return redirect(kite_client(cfg).login_url())


@app.get("/callback")
def callback():
    token = request.args.get("request_token")
    if request.args.get("status") != "success" or not token:
        return redirect("/#login?error=Login+cancelled+or+failed")
    cfg = load_config()
    try:
        kite = kite_client(cfg)
        data = kite.generate_session(token, api_secret=cfg["API_SECRET"])
    except Exception as e:
        return redirect("/#login?error=" + str(e).replace(" ", "+")[:200])
    with open("access_token.txt", "w") as f:
        f.write(data["access_token"])
    acct_cache.update(at=0.0, data=None)
    write_json(TOKEN_META, {"date": today(), "time": datetime.now(IST).strftime("%H:%M"),
                            "user": data.get("user_name") or data.get("user_id")})
    return redirect("/#dashboard")


@app.post("/api/logout")
def logout():
    acct_cache.update(at=0.0, data=None)
    for p in ("access_token.txt", TOKEN_META):
        if os.path.exists(p):
            os.remove(p)
    return jsonify(ok=True)


# ------------------------------------------------------------------ zerodha account (read-only)
acct_cache = {"at": 0.0, "data": None}


@app.get("/api/zerodha/account")
def zerodha_account():
    """Funds, today's orders and positions from Kite. Only read calls — nothing is placed or changed."""
    if not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha to see your account."), 401
    if acct_cache["data"] and time.time() - acct_cache["at"] < 10 and not request.args.get("force"):
        return jsonify(acct_cache["data"])
    cfg = load_config()
    kite = kite_client(cfg)
    with open("access_token.txt") as f:
        kite.set_access_token(f.read().strip())
    try:
        funds = kite.margins()
        orders = kite.orders()
        positions = kite.positions()
    except Exception as e:
        if type(e).__name__ == "TokenException":   # token expired or revoked on Zerodha's side
            return jsonify(ok=False, error="Your Zerodha session has expired. Log in again."), 401
        return jsonify(ok=False, error=f"Zerodha didn't answer: {str(e)[:160]}"), 502

    def seg(name):
        s = funds.get(name) or {}
        av, ut = s.get("available") or {}, s.get("utilised") or {}
        return dict(net=s.get("net", 0), cash=av.get("live_balance", av.get("cash", 0)),
                    opening=av.get("opening_balance", 0), collateral=av.get("collateral", 0),
                    used=ut.get("debits", 0), m2m=ut.get("m2m_realised", 0) + ut.get("m2m_unrealised", 0))

    pos = [dict(symbol=p["tradingsymbol"], exchange=p["exchange"], product=p["product"], qty=p["quantity"],
                avg=p["average_price"], ltp=p["last_price"], pnl=p["pnl"], m2m=p.get("m2m", 0),
                realised=p.get("realised", 0), unrealised=p.get("unrealised", 0))
           for p in positions.get("net", [])]
    ords = [dict(time=str(o.get("order_timestamp") or "")[11:19], symbol=o["tradingsymbol"], exchange=o["exchange"],
                 side=o["transaction_type"], type=o["order_type"], product=o["product"], qty=o["quantity"],
                 filled=o.get("filled_quantity", 0), price=o.get("average_price") or o.get("price") or 0,
                 trigger=o.get("trigger_price", 0), status=o["status"], msg=o.get("status_message") or "",
                 tag=o.get("tag") or "")
            for o in reversed(orders)]
    data = dict(ok=True, fetched=datetime.now(IST).strftime("%H:%M:%S"),
                equity=seg("equity"), commodity=seg("commodity"),
                positions=pos, orders=ords,
                day_pnl=round(sum(p["pnl"] for p in pos), 2),
                open_count=sum(1 for p in pos if p["qty"]))
    acct_cache.update(at=time.time(), data=data)
    return jsonify(data)


# ------------------------------------------------------------------ state
@app.get("/api/state")
def state():
    cfg = load_config()
    risk = read_json("state.json", {})
    if risk.get("date") != today():
        risk = {"date": today(), "pnl": 0, "trades": 0, "consec_losses": 0, "locked": False, "reason": ""}
    status = read_json("status.json", {})
    running = bot_running()
    if not running:
        status = {**status, "state": "stopped", "positions": []}
    return jsonify(
        login=login_state(), running=running, mode=cfg["MODE"], status=status, risk=risk,
        paper_feed=cfg.get("PAPER_FEED", "free"), needs_login=needs_login(cfg),
        backtest_done=os.path.exists("backtest_trades.csv"),
        instruments=cfg.get("TRADE_INSTRUMENTS", "NIFTY"), lots=cfg.get("LOTS", 1),
        limits={k: cfg[k] for k in ("MAX_DAILY_LOSS", "MAX_TRADES_PER_DAY", "MAX_CONSECUTIVE_LOSSES",
                                     "LOTS", "TRADE_INSTRUMENTS", "ITM_STRIKES", "RR",
                                     "USE_CRUDE_AGENT", "USE_NEWS_AGENT", "USE_TRADER_AGENT", "USE_GLOBAL_AGENT",
                                     "VOLUME_FILTER", "VOLUME_MIN_RVOL", "VIX_ADJUST_EXITS")},
        has_key=bool(cfg.get("API_KEY")), has_secret=bool(cfg.get("API_SECRET")),
        stop_pending=os.path.exists("STOP"), now=datetime.now(IST).strftime("%H:%M:%S"),
        started=os.path.getmtime("bot.pid") if running and os.path.exists("bot.pid") else None,
        warmup_need=cfg.get("EMA_TREND", 50) + 10,
        scalp_running=scalp_running(), error=status.get("error") if running else None)


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings():
    cfg, dflt, saved = load_config(), default_config(), read_json(SETTINGS, {})
    fields = []
    for f in SCHEMA:
        k = f["key"]
        v = cfg.get(k, "")
        fields.append(dict(f, value="" if f["type"] == "secret" else v,
                           default="" if f["type"] == "secret" else dflt.get(k, ""),
                           has_value=bool(cfg.get(k)), saved=k in saved,
                           choices=[dict(value=c, label=l) for c, l in f["choices"]] if f["choices"] else None))
    return jsonify(sections=[dict(id=i, title=t, desc=d) for i, t, d in SECTIONS], fields=fields, mode=cfg["MODE"],
                   running=bot_running() or scalp_running(), warnings=cfg.get("TIME_WARNINGS", []),
                   redirect_url=f"http://127.0.0.1:{PORT}/callback")


@app.post("/api/settings")
def save_settings():
    if bot_running() or scalp_running():
        return jsonify(ok=False, error="Stop the bot and the scalper before changing settings."), 409
    data = request.get_json(force=True)
    saved = read_json(SETTINGS, {})
    errors = []
    for k, (typ, _, label) in EDITABLE.items():
        if k not in data:
            continue
        raw = str(data[k]).strip()
        if k == "API_SECRET" and raw == "":
            continue  # blank = keep existing secret
        if typ == "bool":
            saved[k] = raw.lower() in ("true", "1", "on", "yes"); continue
        if isinstance(typ, tuple):
            if raw not in typ:
                errors.append(f"{label}: choose one of {', '.join(typ)}"); continue
            saved[k] = raw; continue
        try:
            val = typ(raw) if typ is not str else raw
        except ValueError:
            errors.append(f"{label}: not a valid number"); continue
        lo, hi = LIMITS.get(k, (None, None))
        if typ in (int, float) and ((lo is not None and val < lo) or (hi is not None and val > hi)):
            rng = f"between {lo:g} and {hi:g}" if lo is not None and hi is not None else f"at least {lo:g}"
            errors.append(f"{label}: must be {rng}"); continue
        if k in ("NO_ENTRY_BEFORE", "NO_ENTRY_AFTER", "SQUARE_OFF_AT", "SCALP_NO_ENTRY_BEFORE", "SCALP_NO_ENTRY_AFTER"):
            try:
                datetime.strptime(val, "%H:%M")
            except ValueError:
                errors.append(f"{label}: use HH:MM"); continue
        saved[k] = val
    if errors:
        return jsonify(ok=False, error="; ".join(errors)), 400
    if saved.get("LOTS", 1) < 1:
        return jsonify(ok=False, error="Lots must be at least 1"), 400
    if not 0 <= saved.get("ITM_STRIKES", 1) <= 5:
        return jsonify(ok=False, error="Strikes in-the-money must be 0-5"), 400
    if saved.get("RR", 2) < 0.5:
        return jsonify(ok=False, error="Reward:risk must be at least 0.5"), 400
    if not 0 <= saved.get("TRADER_BLOCK_MARGIN", 0.35) <= 1:
        return jsonify(ok=False, error="Debate margin must be between 0 and 1"), 400
    mins = lambda v: (lambda h, m: h * 60 + m)(*map(int, v.split(":")))
    sq, last_in = mins(saved.get("SQUARE_OFF_AT", "15:10")), mins(saved.get("NO_ENTRY_AFTER", "13:30"))
    if sq > mins("15:20"):
        return jsonify(ok=False, error="Square off by 15:20 at the latest. Zerodha auto-squares F&O intraday "
                                       "positions at 15:26 for ₹50 + GST per order; after 15:30 the market is shut."), 400
    if last_in >= sq:
        return jsonify(ok=False, error="'No entry after' must be earlier than the square-off time"), 400
    if saved.get("MAX_OPEN_POSITIONS", 1) < 1:
        return jsonify(ok=False, error="Max open positions must be at least 1"), 400
    write_json(SETTINGS, saved)
    return jsonify(ok=True)


@app.post("/api/mode")
def set_mode():
    if bot_running() or scalp_running():
        return jsonify(ok=False, error="Stop the bot and the scalper before switching mode."), 409
    mode = request.get_json(force=True).get("mode")
    if mode not in ("paper", "live"):
        return jsonify(ok=False, error="bad mode"), 400
    saved = read_json(SETTINGS, {})
    saved["MODE"] = mode
    write_json(SETTINGS, saved)
    return jsonify(ok=True, mode=mode)


# ------------------------------------------------------------------ bot control
@app.post("/api/bot/start")
def start_bot():
    global bot_proc
    if bot_running():
        return jsonify(ok=False, error="Bot is already running."), 409
    cfg = load_config()
    if needs_login(cfg) and not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha first (required every day for live or Zerodha-feed paper)."), 400
    env = dict(os.environ)
    if cfg["MODE"] == "live":
        if request.get_json(force=True).get("confirm") != "YES":
            return jsonify(ok=False, error="Type YES to confirm live trading."), 400
        env["BOT_CONFIRM_LIVE"] = "YES"
    if os.path.exists("STOP"):
        os.remove("STOP")
    out = open("bot_console.log", "a")
    bot_proc = subprocess.Popen([sys.executable, "-u", "bot.py"], cwd=BASE, env=env,
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return jsonify(ok=True)


@app.post("/api/bot/stop")
def stop_bot():
    if not bot_running():
        return jsonify(ok=True, msg="Bot was not running.")
    open("STOP", "w").close()  # bot exits any position, then quits

    def reaper(proc):
        for _ in range(60):
            if not bot_running():
                break
            time.sleep(1)
        if os.path.exists("STOP"):
            os.remove("STOP")
    threading.Thread(target=reaper, args=(bot_proc,), daemon=True).start()
    return jsonify(ok=True, msg="Stopping — any open position is being exited.")


@app.get("/api/log")
def get_log():
    return jsonify(log=tail("bot.log", 300), console=tail("bot_console.log", 60))


# ------------------------------------------------------------------ trades & pnl
@app.get("/api/trades")
def trades():
    mode = request.args.get("mode")
    rows = read_trades()
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    return jsonify(trades=list(reversed(rows)))


@app.get("/api/pnl")
def pnl():
    mode = request.args.get("mode")
    rows = read_trades()
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    days = {}
    for r in rows:
        d = days.setdefault(r["date"], {"date": r["date"], "pnl": 0.0, "trades": 0, "wins": 0})
        d["pnl"] += r["net_pnl"]; d["trades"] += 1; d["wins"] += r["net_pnl"] > 0
    daily = sorted(days.values(), key=lambda d: d["date"])
    months = {}
    for d in daily:
        m = months.setdefault(d["date"][:7], {"month": d["date"][:7], "pnl": 0.0, "trades": 0, "wins": 0})
        m["pnl"] += d["pnl"]; m["trades"] += d["trades"]; m["wins"] += d["wins"]
    pnls = [r["net_pnl"] for r in rows]  # everything on this page is after estimated charges
    eq, peak, dd = 0, 0, 0
    for p in pnls:
        eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    summary = {
        "total": sum(pnls), "gross": sum(r["pnl"] for r in rows), "charges": sum(r["charges"] for r in rows),
        "trades": len(pnls), "wins": len(wins),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "best": max(pnls) if pnls else 0, "worst": min(pnls) if pnls else 0,
        "max_dd": dd,
        "best_day": max(daily, key=lambda d: d["pnl"]) if daily else None,
        "worst_day": min(daily, key=lambda d: d["pnl"]) if daily else None,
    }
    return jsonify(daily=daily, monthly=sorted(months.values(), key=lambda m: m["month"]),
                   summary=summary)


# ------------------------------------------------------------------ backtest
@app.post("/api/backtest")
def run_backtest():
    if bt["running"]:
        return jsonify(ok=False, error="A backtest is already running."), 409
    body = request.get_json(force=True)
    days = max(1, min(int(body.get("days", 30)), 55))
    lots = max(1, min(int(body.get("lots", 2)), 50))
    inst = body.get("instrument", "NIFTY")
    if inst not in ("NIFTY", "SENSEX", "BOTH"):
        inst = "NIFTY"
    bt.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)

    def work():
        try:
            p = subprocess.run([sys.executable, "backtest.py", "--days", str(days), "--lots", str(lots), "--instrument", inst],
                               cwd=BASE, capture_output=True, text=True, timeout=600)
            bt["output"] = p.stdout + (("\n" + p.stderr[-3000:]) if p.returncode else "")
        except Exception as e:
            bt["output"] = f"Backtest failed: {e}"
        bt.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/backtest")
def backtest_result():
    rows = []
    if not bt["running"] and os.path.exists("backtest_trades.csv"):
        with open("backtest_trades.csv") as f:
            rows = list(csv.DictReader(f))
    return jsonify(**bt, trades=rows)


# ------------------------------------------------------------------ agents
agent_busy = {"v": False}
agent_lock = threading.Lock()          # one refresh at a time (manual or automatic)
AUTO_EVERY = 60                        # seconds between checks; each agent refetches only when its data is due
auto = {"last": None, "next": None, "errors": []}


def run_refresh(force):
    if not agent_lock.acquire(blocking=False):
        return
    agent_busy["v"] = True
    try:
        from agents import pipeline
        return pipeline.refresh(load_config(), force=force)
    finally:
        agent_busy["v"] = False
        agent_lock.release()


def auto_refresh_loop():
    """Keeps agent readings fresh while the panel is open. Each agent still refetches only when its
    own interval is up (crude 3 min, technical 5, news 10, sentiment 15, fundamentals 12 h). While the
    bot runs it refreshes the agents itself, so this loop stands aside to avoid double requests."""
    time.sleep(5)
    while True:
        try:
            if load_config().get("AGENT_AUTO_REFRESH", True) and not bot_running():
                auto["errors"] = run_refresh(force=False) or []
                auto["last"] = time.time()
        except Exception as e:
            auto["errors"] = [repr(e)[:150]]
        auto["next"] = time.time() + AUTO_EVERY
        time.sleep(AUTO_EVERY)


@app.get("/api/agents")
def agents_get():
    from agents import load_cache, pipeline
    c = load_cache()
    cfg = load_config()
    deb, dec = pipeline.stance(cfg)
    return jsonify(**{k: c.get(k) for k, *_ in pipeline.ANALYSTS}, busy=agent_busy["v"],
                   on={k: bool(cfg.get(flag)) for k, _, flag, _ in pipeline.ANALYSTS},
                   trader_on=bool(cfg.get("USE_TRADER_AGENT")), debate=deb, trader=dec,
                   decisions=(c.get("decisions") or {}).get("items", []),
                   auto=dict(on=bool(cfg.get("AGENT_AUTO_REFRESH", True)), by_bot=bot_running(),
                             last=auto["last"], next=auto["next"], errors=auto["errors"], now=time.time()),
                   crude_on=cfg["USE_CRUDE_AGENT"], news_on=cfg["USE_NEWS_AGENT"])


@app.post("/api/agents/refresh")
def agents_refresh():
    if agent_busy["v"]:
        return jsonify(ok=True)
    threading.Thread(target=run_refresh, args=(True,), daemon=True).start()
    return jsonify(ok=True)


# ------------------------------------------------------------------ live market data
LIVE_INDICES = [  # key, name, Kite symbol, Yahoo symbol, exchange
    ("nifty", "Nifty 50", "NSE:NIFTY 50", "^NSEI", "NSE"),
    ("sensex", "Sensex", "BSE:SENSEX", "^BSESN", "BSE"),
    ("banknifty", "Bank Nifty", "NSE:NIFTY BANK", "^NSEBANK", "NSE"),
    ("vix", "India VIX", "NSE:INDIA VIX", "^INDIAVIX", "NSE"),
]
live_cache = {"at": 0, "data": None}
gift_hist = []                         # GIFT Nifty prices seen while the panel is open (no free intraday series)
live_lock = threading.Lock()


def _kite_quotes(cfg):
    """Real-time index quotes from Kite when logged in today, else {}."""
    if not (cfg.get("API_KEY") and login_state()["logged_in"]):
        return {}
    try:
        k = kite_client(cfg)
        with open("access_token.txt") as f:
            k.set_access_token(f.read().strip())
        q = k.quote([s for _, _, s, _, _ in LIVE_INDICES])
    except Exception:
        return {}
    out = {}
    for key, _, sym, _, _ in LIVE_INDICES:
        d = q.get(sym)
        if d and d.get("last_price"):
            prev = (d.get("ohlc") or {}).get("close") or None
            out[key] = dict(price=d["last_price"], prev_close=prev, source="Zerodha (live)", as_of=time.time(),
                            high=(d.get("ohlc") or {}).get("high"), low=(d.get("ohlc") or {}).get("low"))
    return out


def _yahoo_quote(sym):
    from agents import market
    d = market.chart(sym, "1m", "1d", max_age=10)
    daily = market.chart(sym, "1d", "5d", max_age=300)
    day = datetime.fromtimestamp(d["t"][-1], IST).date()
    prev = [c for t, c in zip(daily["t"], daily["c"]) if datetime.fromtimestamp(t, IST).date() < day]
    return dict(price=d["c"][-1], prev_close=prev[-1] if prev else None, high=max(d["h"]), low=min(d["l"]),
                source="Yahoo (~1 min delay)", as_of=d["t"][-1], spark=_spark(d["c"]))


def _spark(closes, n=60):
    """Today's closes thinned to about n points for a sparkline."""
    step = max(1, len(closes) // n)
    pts = closes[::step]
    if pts and pts[-1] != closes[-1]:
        pts.append(closes[-1])
    return [round(c, 2) for c in pts]


def live_indices():
    """Nifty / Sensex / Bank Nifty / India VIX now, plus GIFT Nifty. Cached 10 s."""
    with live_lock:
        if live_cache["data"] and time.time() - live_cache["at"] < 10:
            return live_cache["data"]
        cfg = load_config()
        kq, rows = _kite_quotes(cfg), []
        for key, name, _, ysym, exch in LIVE_INDICES:
            q = kq.get(key)
            if not q:
                try:
                    q = _yahoo_quote(ysym)
                except Exception as e:
                    q = dict(error=str(e)[:80])
            elif "spark" not in q:
                try:
                    from agents import market
                    q["spark"] = _spark(market.chart(ysym, "1m", "1d", max_age=60)["c"])
                except Exception:
                    pass
            rows.append(dict(key=key, name=name, exchange=exch, **q))
        try:
            from agents import global_markets
            g = global_markets.moneycontrol().get("GIFT NIFTY")
            if g:
                if not gift_hist or gift_hist[-1] != g["price"]:
                    gift_hist.append(g["price"])
                    del gift_hist[:-120]
                rows.insert(2, dict(key="gift", name="GIFT Nifty", exchange="NSE IX", price=g["price"], prev_close=g["prev_close"],
                                    high=g.get("high"), low=g.get("low"), source="Moneycontrol", as_of=g.get("updated"),
                                    spark=list(gift_hist) if len(gift_hist) > 1 else None))
        except Exception as e:
            rows.insert(2, dict(key="gift", name="GIFT Nifty", error=str(e)[:80]))
        for r in rows:
            if r.get("price") is not None and r.get("prev_close"):
                r["change"] = round(r["price"] - r["prev_close"], 2)
                r["change_pct"] = round(r["change"] / r["prev_close"] * 100, 2)
        nifty, gift = (next((r.get("price") for r in rows if r["key"] == k), None) for k in ("nifty", "gift"))
        live_cache.update(at=time.time(), data=dict(indices=rows, gift_premium=round(gift - nifty, 1) if nifty and gift else None))
        return live_cache["data"]


@app.get("/api/markets")
def markets_get():
    """Live index strip + global markets table. Hours/status are recomputed every call so countdowns
    stay current; prices come from the global agent's last reading."""
    from agents import global_markets, load_cache
    g = load_cache().get("global") or {}
    hours = global_markets.sessions()
    rows = [dict(r, **hours.get(r["key"], {})) for r in g.get("markets") or []] or list(hours.values())
    return jsonify(**live_indices(), markets=rows, macro=g.get("macro") or [], read=dict(
        ok=g.get("ok"), score=g.get("score"), label=g.get("label"), note=g.get("note"), bull=g.get("bull") or [],
        bear=g.get("bear") or [], fetched_at=g.get("fetched_at")), on=bool(load_config().get("USE_GLOBAL_AGENT")),
        now=datetime.now(IST).strftime("%H:%M:%S"))


# ------------------------------------------------------------------ scalper
def today_scalps():
    st = read_json("scalp_state.json", {})
    return st if st.get("date") == today() else {"date": today(), "pnl": 0, "trades": 0, "consec_losses": 0,
                                                   "locked": False, "reason": ""}


@app.get("/api/scalp/state")
def scalp_state():
    cfg = load_config()
    status = read_json("scalp_status.json", {})
    running = scalp_running()
    if not running:
        status = {**status, "state": "stopped", "positions": []}
    rows = read_trades("scalp_trades.csv")
    mode = cfg["MODE"]
    mine = [r for r in rows if r.get("mode") == mode]
    return jsonify(running=running, mode=mode, status=status, risk=today_scalps(),
                   allow_live=bool(cfg.get("SCALP_ALLOW_LIVE")), stop_pending=os.path.exists("STOP_SCALP"),
                   needs_login=needs_login(cfg), logged_in=login_state()["logged_in"],
                   rules=dict(on=cfg.get("SCALP_POINTS_ON"), sl=cfg.get("SCALP_SL_POINTS"), tg=cfg.get("SCALP_TARGET_POINTS"),
                              be=cfg.get("SCALP_BE_AT_POINTS"), lots=cfg.get("SCALP_LOTS"), candle=cfg.get("SCALP_CANDLE_MINUTES"),
                              hold=cfg.get("SCALP_MAX_HOLD_MIN"), max_trades=cfg.get("SCALP_MAX_TRADES_PER_DAY"),
                              max_loss=cfg.get("SCALP_MAX_DAILY_LOSS"), streak=cfg.get("SCALP_MAX_CONSECUTIVE_LOSSES"),
                              window=f"{cfg.get('SCALP_NO_ENTRY_BEFORE')}–{cfg.get('SCALP_NO_ENTRY_AFTER')}",
                              strategy=cfg.get("SCALP_STRATEGY"), inst=cfg.get("TRADE_INSTRUMENTS", "NIFTY"),
                              dynamic=bool(cfg.get("SCALP_DYNAMIC_SL")) and cfg.get("SCALP_STRATEGY") == "v2",
                              be_r=cfg.get("SCALP_BE_AT_R")),
                   total=dict(trades=len(mine), pnl=sum(r["pnl"] for r in mine),
                              wins=sum(1 for r in mine if r["pnl"] > 0)))


@app.post("/api/scalp/start")
def scalp_start():
    global scalp_proc
    if scalp_running():
        return jsonify(ok=False, error="The scalper is already running."), 409
    cfg = load_config()
    if needs_login(cfg) and not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha first (required every day for live or Zerodha-feed paper)."), 400
    env = dict(os.environ)
    if cfg["MODE"] == "live":
        if not cfg.get("SCALP_ALLOW_LIVE"):
            return jsonify(ok=False, error="Live scalping is off. The backtest loses money with real fills — turn on "
                                           "'Allow live scalping' in Settings → Scalper only if you accept that."), 400
        if request.get_json(force=True).get("confirm") != "YES":
            return jsonify(ok=False, error="Type YES to confirm live scalping."), 400
        env["BOT_CONFIRM_LIVE"] = "YES"
    if os.path.exists("STOP_SCALP"):
        os.remove("STOP_SCALP")
    out = open("scalp_console.log", "a")
    scalp_proc = subprocess.Popen([sys.executable, "-u", "scalper.py"], cwd=BASE, env=env,
                                  stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return jsonify(ok=True)


@app.post("/api/scalp/stop")
def scalp_stop():
    if not scalp_running():
        return jsonify(ok=True, msg="The scalper was not running.")
    open("STOP_SCALP", "w").close()

    def reaper(proc):
        for _ in range(60):
            if not scalp_running():
                break
            time.sleep(1)
        if os.path.exists("STOP_SCALP"):
            os.remove("STOP_SCALP")
    threading.Thread(target=reaper, args=(scalp_proc,), daemon=True).start()
    return jsonify(ok=True, msg="Stopping — any open scalp is being exited.")


@app.get("/api/scalp/trades")
def scalp_trades():
    mode = request.args.get("mode")
    rows = read_trades("scalp_trades.csv")
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    return jsonify(trades=list(reversed(rows)))


@app.get("/api/scalp/log")
def scalp_log():
    return jsonify(log=tail("scalp.log", 200), console=tail("scalp_console.log", 40))


@app.post("/api/scalp/backtest")
def scalp_backtest_run():
    if sbt["running"]:
        return jsonify(ok=False, error="A scalp backtest is already running."), 409
    body = request.get_json(force=True)
    inst = body.get("instrument", "NIFTY")
    inst = inst if inst in ("NIFTY", "SENSEX", "BOTH") else "NIFTY"
    lots = max(1, min(int(body.get("lots", 1)), 50))
    strat = body.get("strategy", "both")
    strat = strat if strat in ("ema", "volume", "both", "v2") else "both"
    sbt.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)

    def work():
        try:
            cmd = ([sys.executable, "scalp_v2_backtest.py", "--quick", "--instrument", inst] if strat == "v2" else
                   [sys.executable, "scalp_backtest.py", "--instrument", inst, "--lots", str(lots), "--strategy", strat])
            p = subprocess.run(cmd,
                               cwd=BASE, capture_output=True, text=True, timeout=900)
            lines = [l for l in p.stdout.splitlines() if "delisted" not in l and "Failed download" not in l]
            sbt["output"] = "\n".join(lines) + (("\n" + p.stderr[-2000:]) if p.returncode else "")
        except Exception as e:
            sbt["output"] = f"Scalp backtest failed: {e}"
        sbt.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/scalp/backtest")
def scalp_backtest_result():
    return jsonify(**sbt)


# ------------------------------------------------------------------ strategy lab
lab = {"running": False, "output": "", "started": None, "finished": None}
LAB_RESULTS = os.path.join("reports", "scalp_lab", "results.json")


@app.get("/api/lab")
def lab_get():
    res = read_json(LAB_RESULTS, None)
    if res:
        res.get("walk_forward", {}).pop("rows", None)
    return jsonify(results=res, **lab)


@app.post("/api/lab/run")
def lab_run():
    if lab["running"]:
        return jsonify(ok=False, error="The lab is already running."), 409
    lab.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)

    def work():
        try:
            p = subprocess.run([sys.executable, "-W", "ignore", "-m", "scalp_lab.experiments"], cwd=BASE,
                               capture_output=True, text=True, timeout=1800)
            lab["output"] = (p.stdout + p.stderr)[-6000:]
        except Exception as e:
            lab["output"] = f"Lab failed: {e!r}"
        lab.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


# ------------------------------------------------------------------ shutdown
@app.post("/api/shutdown")
def shutdown():
    if bot_running() or scalp_running():
        return jsonify(ok=False, error="Stop the bot and the scalper first."), 409
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return jsonify(ok=True)


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    print(f"\n  Kite Bot control panel: {url}\n  Keep this window open. Close it to shut everything down.\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
