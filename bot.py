"""
Nifty / Sensex options bot for Zerodha Kite Connect — strategy v2.

Signal : strategy.signal_v2 on 5-min index candles (trend + momentum + confirmation)
Option : buys an IN-THE-MONEY option (ITM_STRIKES deep), nearest weekly expiry
Exits  : stop / 1:RR target / breakeven trail measured on the INDEX, or square-off
Filters: volume confirmation (NIFTYBEES proxy) and India-VIX-scaled stop/target (VOLUME_FILTER, VIX_ADJUST_EXITS)
Agents : analysts (fundamental, sentiment, news, technical, crude, global markets) → bull/bear debate →
         trader (BUY CE / BUY PE / HOLD) → risk manager (final approval). See agents/pipeline.py.
         Agents can only stop a trade; only the strategy signal can start one.
Safety : daily loss cap, max trades, losses-in-a-row lock, max open positions,
         exchange-side disaster stop in live mode, STOP-file kill switch, limit orders only.

Run via the control panel (app.py), or:  python login.py  then  python bot.py
"""
import csv, json, os, sys, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import config
import strategy
from agents import debate, load_cache, market, pipeline, risk_manager, technical, trader

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"


def now():
    return datetime.now(IST)


def hm(s):
    h, m = map(int, s.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


def log(*a):
    msg = f"[{now():%H:%M:%S}] " + " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open("bot.log", "a") as f:
        f.write(f"{now():%Y-%m-%d} {msg}\n")


def tick_round(p):
    return round(round(p / 0.05) * 0.05, 2)


# ------------------------------------------------------------------ risk state
class Risk:
    """Persists for the whole day, so restarting the bot does NOT reset limits."""

    def __init__(self):
        self.s = {"date": str(date.today()), "pnl": 0.0, "trades": 0,
                  "consec_losses": 0, "locked": False, "reason": ""}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if saved.get("date") == self.s["date"]:
                self.s = saved

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.s, f, indent=2)

    def record(self, pnl):
        self.s["pnl"] += pnl
        self.s["trades"] += 1
        self.s["consec_losses"] = self.s["consec_losses"] + 1 if pnl < 0 else 0
        if self.s["pnl"] <= -config.MAX_DAILY_LOSS:
            self.lock(f"daily loss limit hit (₹{self.s['pnl']:.0f})")
        elif self.s["consec_losses"] >= config.MAX_CONSECUTIVE_LOSSES:
            self.lock(f"{self.s['consec_losses']} losses in a row")
        elif self.s["trades"] >= config.MAX_TRADES_PER_DAY:
            self.lock("max trades for the day reached")
        self.save()

    def lock(self, reason):
        self.s["locked"], self.s["reason"] = True, reason
        log("RISK LOCK:", reason, "- no more trades today.")
        self.save()

    def can_trade(self):
        return not self.s["locked"]


# ------------------------------------------------------------------ broker
TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED")
STOP_OPEN = ("TRIGGER PENDING", "OPEN")


class Fill:
    """What actually happened to an order: average price and FILLED quantity (not requested quantity).
    status is the order's last Kite status, or STOP_STILL_OPEN when sell() found the exchange stop
    could not be cancelled and therefore sent nothing (a second sell could leave you short)."""

    def __init__(self, price, qty, status="COMPLETE", oid=None):
        self.price, self.qty, self.status, self.oid = price, int(qty or 0), status, oid

    def __bool__(self):
        return self.qty > 0

    def __repr__(self):
        return f"Fill({self.price}, {self.qty}, {self.status}, {self.oid})"


class Broker:
    def __init__(self, kite, live):
        self.kite, self.live = kite, live
        self._instruments = {}
        self._seq = 0
        self._stop_trigger = {}  # sl order id -> current trigger; stops may only move up (we hold long options)

    def ltp(self, key):
        return self.kite.ltp([key])[key]["last_price"]

    def option_for(self, spec, side, spot):
        """Nearest-expiry ITM option for this index."""
        ex = spec["exchange"]
        if ex not in self._instruments:
            self._instruments[ex] = [i for i in self.kite.instruments(ex)
                                     if i["name"] == spec["name"] and i["instrument_type"] in ("CE", "PE")]
        strike = strategy.itm_strike(spot, side, spec["step"], config.ITM_STRIKES)
        today = date.today()
        cands = [i for i in self._instruments[ex]
                 if i["instrument_type"] == side and float(i["strike"]) == strike and i["expiry"] >= today]
        if not cands:
            return None
        return min(cands, key=lambda i: i["expiry"])

    # ---- order plumbing (live only)
    def _new_tag(self):
        """Unique per order (Kite tags: alphanumeric, max 20), so a request that errored can be looked up."""
        self._seq += 1
        return f"{getattr(self, 'tag', 'kitebot')[:9]}{int(time.time()) % 10 ** 8:08d}{self._seq % 100:02d}"

    def _find_by_tag(self, tag):
        for _ in range(3):
            try:
                for o in self.kite.orders():
                    if o.get("tag") == tag or tag in (o.get("tags") or []):
                        return o["order_id"]
            except Exception as e:
                log("Order lookup failed:", e)
            time.sleep(1)
        return None

    def _order(self, ex, sym, side, qty, **kw):
        if not self.live:
            raise RuntimeError("paper mode: refusing to send a broker order")
        tag = self._new_tag()
        try:
            return self.kite.place_order(variety="regular", exchange=ex, tradingsymbol=sym, transaction_type=side,
                                         quantity=qty, product="MIS", tag=tag, **kw)
        except Exception as e:
            # a timeout can still have reached Zerodha — never send the same order twice
            oid = self._find_by_tag(tag)
            if oid:
                log(f"Order call errored ({e}) but Zerodha has it as {oid} — using that one.")
                return oid
            raise

    def order_state(self, oid):
        h = self.kite.order_history(oid)[-1]
        return dict(status=h["status"], filled=int(h.get("filled_quantity") or 0),
                    avg=h.get("average_price") or 0.0, msg=h.get("status_message"))

    def _settle(self, oid, secs=10):
        """Poll until the order reaches a final status (or give up after secs)."""
        st = self.order_state(oid)
        t0 = time.time()
        while st["status"] not in TERMINAL and time.time() - t0 < secs:
            time.sleep(1)
            st = self.order_state(oid)
        return st

    def _cancel(self, oid):
        try:
            self.kite.cancel_order("regular", oid)
        except Exception as e:
            log("Cancel of", oid, "failed (may already be filled):", e)
        return self._settle(oid)

    def _wait_fill(self, order_id, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.order_state(order_id)
            if st["status"] == "COMPLETE":
                return Fill(st["avg"], st["filled"], "COMPLETE", order_id)
            if st["status"] in ("REJECTED", "CANCELLED"):
                log("Order", order_id, st["status"], st["msg"], f"(filled {st['filled']})")
                return Fill(st["avg"] or None, st["filled"], st["status"], order_id)
            time.sleep(1)
        st = self._cancel(order_id)
        if st["status"] not in TERMINAL:
            log("!!! Order", order_id, "could not be cancelled (status", st["status"], ") — CHECK KITE !!!")
        elif st["filled"]:
            log(f"Order {order_id} only partly filled ({st['filled']}) before cancel.")
        else:
            log("Order not filled in time, cancelled.")
        return Fill(st["avg"] or None, st["filled"], st["status"], order_id)

    # ---- trading calls
    def buy(self, ex, sym, qty, ref):
        if not self.live:
            return Fill(ref, qty)
        oid = self._order(ex, sym, "BUY", qty, order_type="LIMIT", price=tick_round(ref * 1.01))
        return self._wait_fill(oid)

    def move_stop(self, sl_id, trigger):
        """Raise the exchange-side stop (e.g. to cost at 1:1) so it protects the gain if the bot dies.
        Refuses to lower it: the bot only holds long options, so a lower sell-stop means more risk."""
        if not self.live or not sl_id:
            return
        trigger = tick_round(trigger)
        if trigger <= self._stop_trigger.get(sl_id, 0):
            log(f"Not moving the exchange stop down ({self._stop_trigger[sl_id]} → {trigger}).")
            return
        try:
            self.kite.modify_order("regular", sl_id, trigger_price=trigger, price=tick_round(trigger * 0.97))
            self._stop_trigger[sl_id] = trigger
        except Exception as e:
            log("Could not move the exchange stop:", e)

    def disaster_stop(self, ex, sym, qty, trigger):
        """Exchange-side stop far below the bot's own stop — protects you if the bot/PC dies."""
        if not self.live or trigger <= 0.5:
            return None
        oid = self._order(ex, sym, "SELL", qty, order_type="SL",
                          trigger_price=tick_round(trigger), price=tick_round(trigger * 0.95))
        self._stop_trigger[oid] = tick_round(trigger)
        return oid

    def sl_filled(self, sl_id):
        if not self.live or not sl_id:
            return None
        st = self.order_state(sl_id)
        return Fill(st["avg"], st["filled"], st["status"], sl_id) if st["status"] == "COMPLETE" else None

    def sell(self, ex, sym, qty, ref, sl_id=None):
        """Exit qty units. Returns a Fill for what was actually sold (exchange stop + exit order combined)."""
        if not self.live:
            return Fill(ref, qty)
        done, value, status, oid = 0, 0.0, "COMPLETE", None
        if sl_id:
            st = self._cancel(sl_id)
            done, value = st["filled"], st["filled"] * st["avg"]
            if st["status"] not in TERMINAL:
                log("!!! Exchange stop", sl_id, "is still live and could not be cancelled — not sending another sell")
                return Fill(st["avg"] or None, done, "STOP_STILL_OPEN", sl_id)
        rest = qty - done
        if rest > 0:
            try:
                oid = self._order(ex, sym, "SELL", rest, order_type="LIMIT", price=tick_round(ref * 0.98))
                f = self._wait_fill(oid, timeout=20)
                done, value, status = done + f.qty, value + f.qty * (f.price or 0), f.status
            except Exception as e:
                log("Exit order error:", e)
                status = "ERROR"
        if done < qty:
            log(f"!!! EXIT INCOMPLETE for {sym}: sold {done} of {qty} — the bot keeps trying; check Kite !!!")
        return Fill(value / done if done else None, done, "COMPLETE" if done >= qty else status, oid)


# ------------------------------------------------------------------ volume & VIX
def candle_volume(c):
    """Relative volume of a just-closed index candle, from VOLUME_SYMBOL's 1-min bars (indices have no
    volume). Returns (rvol or None, note)."""
    if c.get("t") is None:
        return None, "candle has no time"
    try:
        d = market.chart(config.VOLUME_SYMBOL, "1m", "5d", max_age=15)
    except Exception as e:
        return None, f"volume data unavailable ({str(e)[:60]})"
    rv, n = strategy.relative_volume(d["t"], d["v"], c["t"].timestamp(), config.CANDLE_MINUTES, config.VOLUME_AVG_N)
    return rv, f"{n} of {config.CANDLE_MINUTES} min of volume published"


def india_vix(broker):
    """India VIX now: Kite quote when the feed has it, else Yahoo, else the sentiment agent's last reading."""
    for fn in (lambda: broker.ltp("NSE:INDIA VIX"), lambda: market.chart("^INDIAVIX", "1m", "1d", max_age=60)["c"][-1],
               lambda: (load_cache().get("sentiment") or {})["vix"]):
        try:
            v = float(fn())
            if 5 < v < 100:
                return v
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ candles
class Candles:
    """Builds OHLC candles from polled prices."""

    def __init__(self, minutes):
        self.m, self.done, self.cur, self.cur_start = minutes, [], None, None

    def bucket(self, t):
        mins = (t.hour * 60 + t.minute) // self.m * self.m
        return t.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)

    def update(self, t, p):
        """Returns True when a candle has just closed."""
        b = self.bucket(t)
        closed = False
        if self.cur_start is not None and b != self.cur_start:
            self.done.append(self.cur)
            self.done = self.done[-400:]
            closed = True
        if self.cur_start != b:
            self.cur_start, self.cur = b, dict(o=p, h=p, l=p, c=p, t=b)
        else:
            self.cur.update(h=max(self.cur["h"], p), l=min(self.cur["l"], p), c=p)
        return closed


def warmup(kite, spec, candles):
    try:
        data = kite.historical_data(spec["token"], now() - timedelta(days=6), now(),
                                    f"{config.CANDLE_MINUTES}minute")
        cur = candles.bucket(now())
        candles.done = [dict(o=c["open"], h=c["high"], l=c["low"], c=c["close"]) for c in data
                        if c["date"].astimezone(IST) < cur][-400:]
        log(f"{spec['name']}: warm-up loaded {len(candles.done)} candles.")
    except Exception as e:
        need = (config.EMA_TREND + 10) * config.CANDLE_MINUTES
        log(f"{spec['name']}: no historical data ({e}). Building live; first signal in ~{need} min.")




def write_status(**kw):
    kw["updated"] = now().strftime("%Y-%m-%d %H:%M:%S")
    kw["mode"] = config.MODE
    tmp = "status.json.tmp"
    with open(tmp, "w") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, "status.json")


TRADE_COLS = ["date", "mode", "symbol", "qty", "entry_time", "entry", "exit_time", "exit", "pnl", "reason",
              "index", "side", "idx_entry", "idx_sl", "idx_target", "idx_exit", "why", "charges", "net_pnl"]


def write_trade(row):
    new = not os.path.exists(TRADES_FILE)
    if not new:  # migrate an older header if needed
        with open(TRADES_FILE) as f:
            header = f.readline().strip().split(",")
        if header != TRADE_COLS:
            with open(TRADES_FILE) as f:
                old = list(csv.DictReader(f))
            with open(TRADES_FILE, "w", newline="") as f:
                w = csv.DictWriter(f, TRADE_COLS); w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k, "") for k in TRADE_COLS})
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------------ restart safety
def pid_alive(pid, script):
    """True if pid is a running python process for script (guards against recycled pids)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    import subprocess
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return script in out


def acquire_lock(path, script):
    """One copy per bot — a second copy would double every order. False if another copy is running."""
    try:
        with open(path) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and pid_alive(pid, script):
        return False
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    import atexit

    def release():
        try:
            with open(path) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(path)
        except OSError:
            pass
    atexit.register(release)
    return True


def keep_awake():
    """Stop macOS idle-sleeping while this bot runs. A sleeping Mac gets no prices, so candles are
    built across the gap and skipped, and signals the backtest takes are silently missed.
    caffeinate -w exits by itself when this process ends. Closing the lid still sleeps the Mac."""
    if sys.platform != "darwin":
        return None
    import subprocess
    try:
        return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"Could not stop the Mac sleeping ({e}) — keep it awake yourself or signals will be missed.")
        return None


POSITIONS_FILE = "positions.json"


def save_positions(positions, path=None):
    data = dict(date=str(date.today()), mode=config.MODE,
                positions={n: dict(p, t=p["t"].isoformat()) for n, p in positions.items()})
    path = path or POSITIONS_FILE
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(path + ".tmp", path)


def load_positions(path=None):
    """Today's open positions for the current mode (paper positions never resume in live, or vice versa)."""
    try:
        with open(path or POSITIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if data.get("date") != str(date.today()) or data.get("mode") != config.MODE:
        return {}
    out = {}
    for n, p in (data.get("positions") or {}).items():
        p["t"] = datetime.fromisoformat(p["t"])
        out[n] = p
    return out


def reconcile(broker, saved, exchanges):
    """Live restart: compare the saved positions with Zerodha's MIS positions.
    Returns (positions to keep managing, [(name, pos, row)] closed while offline, [problems])."""
    rows = {r["tradingsymbol"]: r for r in broker.kite.positions().get("net", [])
            if r.get("product") == "MIS" and r.get("exchange") in exchanges}
    keep, closed, problems = {}, [], []
    for n, p in saved.items():
        r = rows.pop(p["sym"], None)
        q = int(r.get("quantity") or 0) if r else 0
        if r is None:
            problems.append(f"{p['sym']} was open but Zerodha shows no position — check Kite")
        elif q == 0:
            closed.append((n, p, r))
        elif q < 0:
            problems.append(f"{p['sym']}: Zerodha shows a SHORT position ({q}) — exit it manually")
        else:
            if q != p["qty"]:
                problems.append(f"{p['sym']}: Zerodha holds {q}, the bot had {p['qty']} — now tracking {q}")
                p["qty"] = q
            if p.get("sl_id"):
                try:
                    status = broker.order_state(p["sl_id"])["status"]
                except Exception:
                    status = "unknown"
                if status in STOP_OPEN:
                    broker._stop_trigger[p["sl_id"]] = p.get("sl_trigger", 0)
                else:
                    p["sl_id"] = None  # re-placed by the main loop
            keep[n] = p
    for sym, r in rows.items():
        if int(r.get("quantity") or 0):
            problems.append(f"{sym}: {r['quantity']} open at Zerodha but not opened by this bot — not managed")
    return keep, closed, problems


def is_fatal(e):
    """Errors that retrying can't fix (Kite plan lacks market data, login expired)."""
    return type(e).__name__ in ("PermissionException", "TokenException")


# ------------------------------------------------------------------ main loop
def main():
    live = config.MODE == "live"
    if not acquire_lock("bot.pid", "bot.py"):
        log("Another copy of the bot is already running — not starting a second one (it would double every order).")
        sys.exit(1)
    if keep_awake():
        log("Keeping the Mac awake while the bot runs (closing the lid will still sleep it).")
    for w in config.TIME_WARNINGS:
        log(w)
    if live:
        ok = os.environ.get("BOT_CONFIRM_LIVE") or input("LIVE MODE - real money. Type YES to continue: ")
        if ok.strip() != "YES":
            sys.exit("Aborted.")

    if not live and config.PAPER_FEED == "free":
        from feed import FreeFeed
        kite = FreeFeed()
        log("Paper feed: FREE (Yahoo index prices, modelled option premiums — no Zerodha keys needed)")
    else:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=config.API_KEY)
        with open("access_token.txt") as f:
            kite.set_access_token(f.read().strip())
    broker, risk = Broker(kite, live), Risk()
    names = config.instruments()
    specs = {n: config.INSTRUMENT_SPECS[n] for n in names}
    candles = {n: Candles(config.CANDLE_MINUTES) for n in names}
    log(f"Started v2 in {config.MODE.upper()} mode on {', '.join(names)} | ITM {config.ITM_STRIKES} | "
        f"1:{config.RR:g} | agents on: "
        + (", ".join(k for k, _, flag, _ in pipeline.ANALYSTS if getattr(config, flag)) or "none")
        + f" | trader {'on' if config.USE_TRADER_AGENT else 'off'}")

    positions = load_positions()  # name -> dict; saved on every change so a restart resumes them
    spots, last_ok, gap_bucket = {}, {}, {}
    plist, errors = [], 0

    def book_exit(name, exit_px, qty, reason, idx_exit, t=None):
        """Record a fully closed position: trade row, net P&L into the risk limits, forget it."""
        p = positions.pop(name)
        gross = (exit_px - p["entry"]) * qty
        ch = strategy.charges(p["entry"], exit_px, qty, p["exchange"])
        log(f"EXIT {p['sym']} @ {exit_px:.2f} ({reason}; {name} {idx_exit:.1f}) "
            f"P&L ₹{gross:.0f}, net ₹{gross - ch:.0f} after ≈₹{ch:.0f} charges")
        write_trade(dict(date=date.today(), mode=config.MODE, symbol=p["sym"], qty=qty,
                         entry_time=f"{p['t']:%H:%M}", entry=p["entry"], exit_time=f"{(t or now()):%H:%M}",
                         exit=round(exit_px, 2), pnl=round(gross, 2), reason=reason, index=name, side=p["side"],
                         idx_entry=round(p["idx_entry"], 2), idx_sl=round(p["sl0"], 2),
                         idx_target=round(p["target"], 2), idx_exit=round(idx_exit, 2), why=p["why"],
                         charges=ch, net_pnl=round(gross - ch, 2)))
        risk.record(gross - ch)
        save_positions(positions)

    def close(name, reason, idx_exit):
        """Exit a position. A partial exit keeps the rest open (and locks new entries) until it is sold."""
        p = positions[name]
        px = broker.ltp(p["key"])
        f = broker.sell(p["exchange"], p["sym"], p["qty"], px, p.get("sl_id"))
        if f.status != "STOP_STILL_OPEN":
            p["sl_id"] = None  # sell() cancelled (or found filled) the exchange stop
        if f.qty:
            p["sold"] = p.get("sold", 0) + f.qty
            p["sold_value"] = p.get("sold_value", 0.0) + f.qty * f.price
            p["qty"] -= f.qty
        if p["qty"] > 0:
            risk.lock(f"exit of {p['sym']} incomplete ({p['qty']} left) - bot keeps trying; check Kite")
            save_positions(positions)
            return
        book_exit(name, p["sold_value"] / p["sold"], p["sold"], reason, idx_exit)

    def ensure_stop(p):
        """Live: keep an exchange-side stop on every open position; retry every 30 s if placing it failed."""
        if not live or p.get("sl_id") or time.time() - p.get("sl_try", 0) < 30:
            return
        p["sl_try"] = time.time()
        try:
            p["sl_id"] = broker.disaster_stop(p["exchange"], p["sym"], p["qty"], p["dstop"])
            p["sl_trigger"] = broker._stop_trigger.get(p["sl_id"], p["dstop"])
            if p["sl_id"]:
                log(f"Exchange stop placed for {p['sym']} (trigger {p['sl_trigger']})")
        except Exception as e:
            log(f"!!! Exchange stop for {p['sym']} not placed ({e}) — the bot's own stop is active; retrying !!!")
        save_positions(positions)

    if positions:
        log("Resuming open positions from before the restart:", ", ".join(p["sym"] for p in positions.values()))
    if live:
        try:
            positions, closed, problems = reconcile(broker, positions, {s["exchange"] for s in specs.values()})
        except Exception as e:
            closed, problems = [], [f"could not read positions from Zerodha ({e!r}) — check Kite"]
        for n, p, r in closed:
            positions[n] = p
            px = float(r.get("sell_price") or 0) or p["entry"]
            book_exit(n, px, p["qty"], "closed while bot was offline", spots.get(n, p["idx_entry"]))
        for pr in problems:
            log("RESTART CHECK:", pr)
        if problems:
            risk.lock("restart check: " + problems[0])
    save_positions(positions)

    for n in names:
        warmup(kite, specs[n], candles[n])
        gap_bucket[n] = candles[n].bucket(now())  # the candle in progress at start has no true open

    # agents refresh in a background thread so slow websites never delay exits
    import threading

    def agent_loop():
        while True:
            for e in pipeline.refresh(config):
                log("Agent error:", e)
            time.sleep(60)
    threading.Thread(target=agent_loop, daemon=True).start()

    while True:
        try:
            t = now()
            if os.path.exists("STOP"):
                log("STOP file found.")
                for _ in range(3):
                    for n in list(positions):
                        close(n, "kill switch", spots.get(n, 0))
                    if not positions:
                        break
                    time.sleep(5)
                if positions:
                    log("!!! Could not exit", list(positions), "- EXIT MANUALLY IN KITE !!!")
                break
            if t.weekday() >= 5 or t < hm("09:15"):
                write_status(state="waiting for market open", positions=[], spots={}, risk=risk.s,
                             instruments=names)
                time.sleep(30); continue
            if t >= hm(config.SQUARE_OFF_AT):
                for n in list(positions):
                    close(n, "square-off time", broker.ltp(specs[n]["index"]))
                if positions and t < hm("15:25"):
                    write_status(state="square-off: exit pending", positions=plist, spots=spots, risk=risk.s,
                                 instruments=names)
                    time.sleep(config.POLL_SECONDS); continue
                if positions:
                    log("!!! Square-off failed for", list(positions), "- EXIT MANUALLY IN KITE NOW !!!")
                log(f"Day done. P&L ₹{risk.s['pnl']:.0f} net, trades {risk.s['trades']}.")
                write_status(state="day finished", positions=[], spots=spots, risk=risk.s, instruments=names)
                break

            for n in names:
                spec = specs[n]
                spot = broker.ltp(spec["index"])
                if n in last_ok and (t - last_ok[n]).total_seconds() > max(60, 6 * config.POLL_SECONDS):
                    gap_bucket[n] = candles[n].bucket(t)
                    log(f"{n}: no price for {(t - last_ok[n]).seconds} s — no entries until a clean candle.")
                last_ok[n] = t
                spots[n] = round(spot, 2)
                new_candle = candles[n].update(t, spot)

                # ---- manage open position on index levels
                if n in positions:
                    p = positions[n]
                    ensure_stop(p)
                    f = broker.sl_filled(p.get("sl_id"))
                    if f:  # disaster stop hit at exchange
                        p["sl_id"] = None
                        book_exit(n, f.price, f.qty, "disaster stop", spot, t)
                    else:
                        was_be = p["be_done"]
                        lvl, why = strategy.manage(p, spot, spot, config, now=t, close=spot)
                        if lvl is not None:
                            close(n, why, spot)
                        elif p["be_done"] and not was_be:
                            cost_px = p["entry"] + p["cost_unit"]
                            log(f"{n}: 1:1 reached — stop moved to cost (index {p['sl']:.1f}, "
                                f"option ≈ {cost_px:.2f} incl. charges)")
                            broker.move_stop(p.get("sl_id"), cost_px * 0.98)
                            p["sl_trigger"] = broker._stop_trigger.get(p.get("sl_id"), p.get("sl_trigger"))
                            save_positions(positions)
                        elif p["be_done"] and config.COST_STOP_ON_PREMIUM:
                            if broker.ltp(p["key"]) <= p["entry"] + p["cost_unit"]:
                                close(n, "stop at cost", spot)

                # ---- new entries on candle close
                if not new_candle or n in positions:
                    continue
                if n in gap_bucket and candles[n].done[-1].get("t") and candles[n].done[-1]["t"] <= gap_bucket[n]:
                    continue  # this candle was built across a price gap
                if not (risk.can_trade() and hm(config.NO_ENTRY_BEFORE) <= t <= hm(config.NO_ENTRY_AFTER)):
                    continue
                if len(positions) >= config.MAX_OPEN_POSITIONS:
                    continue
                sig = strategy.signal_v2(candles[n].done, config)
                if not sig:
                    continue
                if config.VOLUME_FILTER:
                    rv, note = candle_volume(candles[n].done[-1])
                    if rv is None:
                        log(f"{n}: {sig['side']} signal — volume check skipped: {note}")
                    elif rv < config.VOLUME_MIN_RVOL:
                        log(f"{n}: {sig['side']} signal skipped — volume {rv:.2f}x normal, needs {config.VOLUME_MIN_RVOL:g}x ({note})")
                        pipeline.record(n, sig, dict(margin=0.0, verdict="not run", flags=[], challenges=[]),
                                        dict(action="SKIP", reason="low volume"), None,
                                        f"skipped — volume {rv:.2f}x normal (needs {config.VOLUME_MIN_RVOL:g}x)")
                        continue
                    else:
                        sig = dict(sig, rvol=round(rv, 2), why=sig["why"] + f", volume {rv:.1f}x")
                if config.VIX_ADJUST_EXITS:
                    vix = india_vix(broker)
                    if vix:
                        sig = strategy.vix_scale(sig, vix, config)
                    else:
                        log(f"{n}: India VIX unavailable — stop/target not VIX-adjusted")
                # ---- agent team: analysts → debate → trader → risk manager
                reports = pipeline.reports(config)
                if config.USE_TECHNICAL_AGENT:  # fresh read on this index's own candles
                    reports["technical"] = technical.analyse(candles[n].done, n.title())
                deb = debate.run(reports, sig["side"])
                dec = trader.decide(sig, deb, config)
                if config.USE_TRADER_AGENT and dec["action"] == "HOLD":
                    log(f"{n}: {sig['side']} signal — trader HOLD: {dec['reason']}")
                    pipeline.record(n, sig, deb, dec, None, "held by trader")
                    continue
                inst = broker.option_for(spec, sig["side"], spot)
                if not inst:
                    log(f"{n}: no ITM option found for {sig['side']}"); continue
                key = f"{spec['exchange']}:{inst['tradingsymbol']}"
                px = broker.ltp(key)
                qty = inst["lot_size"] * config.LOTS
                rm = risk_manager.review(sig, qty, px, risk.s, len(positions), reports.get("crude"),
                                         reports.get("news"), reports.get("fundamental"), config, reports.get("global"))
                if not rm["approved"]:
                    log(f"{n}: {sig['side']} {inst['tradingsymbol']} vetoed by risk manager — {rm['reason']}")
                    pipeline.record(n, sig, deb, dec, rm, "vetoed by risk manager")
                    continue
                log(f"{n}: trader {dec['action']} (conviction {dec['conviction']:.2f}, debate {deb['margin']:+.2f}); "
                    f"risk manager approved")
                fill = broker.buy(spec["exchange"], inst["tradingsymbol"], qty, px)
                if fill.status not in TERMINAL:
                    risk.lock(f"buy order {fill.oid} still open after cancel - check Kite")
                if not fill:
                    pipeline.record(n, sig, deb, dec, rm, "order not filled")
                    continue
                if fill.qty < qty:
                    log(f"{n}: partial fill — holding {fill.qty} of {qty}")
                qty = fill.qty
                pipeline.record(n, sig, deb, dec, rm, f"bought {inst['tradingsymbol']} x{qty} @ {fill.price:.2f}")
                # re-anchor index levels to the live spot at fill; record the position BEFORE the exchange stop,
                # so a failed stop order can never leave a bought option untracked
                shift = spot - sig["entry"]
                positions[n] = dict(sym=inst["tradingsymbol"], key=key, exchange=spec["exchange"], qty=qty,
                                    entry=fill.price, side=sig["side"], t=t, idx_entry=spot,
                                    sl=sig["sl"] + shift, sl0=sig["sl"] + shift, target=sig["target"] + shift,
                                    risk=sig["risk"], be_done=False, why=sig["why"], best=spot,
                                    cost_pts=strategy.cost_points(fill.price, qty),
                                    cost_unit=strategy.cost_per_unit(fill.price, qty),
                                    dstop=fill.price - 1.0 * sig["risk"],  # ITM delta ~0.6-0.7: well beyond the bot's stop
                                    sl_id=None, buy_oid=fill.oid)
                save_positions(positions)
                p = positions[n]
                ensure_stop(p)
                log(f"ENTRY {sig['side']} {p['sym']} x{qty} @ {fill.price:.2f} | {n} {spot:.1f} "
                    f"SL {p['sl']:.1f} TGT {p['target']:.1f} (1:{config.RR:g}) | {sig['why']}")

            # ---- status for dashboard
            plist = []
            for n, p in positions.items():
                ltp = broker.ltp(p["key"])
                plist.append(dict(index=n, symbol=p["sym"], side=p["side"], qty=p["qty"], entry=round(p["entry"], 2),
                                  ltp=ltp, pnl=round((ltp - p["entry"]) * p["qty"], 2), since=p["t"].strftime("%H:%M"),
                                  idx=spots.get(n), idx_sl=round(p["sl"], 1), idx_target=round(p["target"], 1),
                                  breakeven=p["be_done"], exchange_stop=bool(p.get("sl_id")) if live else None))
            errors = 0
            write_status(state="running", positions=plist, spots=spots, risk=risk.s, instruments=names,
                         candles={n: len(candles[n].done) for n in names},
                         last_price={n: f"{v:%H:%M:%S}" for n, v in last_ok.items()})
            time.sleep(config.POLL_SECONDS)
        except KeyboardInterrupt:
            log("Ctrl+C - stopping.")
            if positions:
                log("!!! Open positions", list(positions), "- exit manually in Kite or rerun bot !!!")
            break
        except Exception as e:
            errors += 1
            if errors == 1 or errors % 30 == 0:
                log(f"Error ({errors} in a row):", repr(e))
            if is_fatal(e) and not positions:
                msg = ("Zerodha refused the call: " + str(e) + " — your Kite Connect plan may not include market "
                       "data, or the login expired. Log in again, or use Paper feed = free.")
                log(msg)
                write_status(state="error", error=msg, positions=[], spots=spots, risk=risk.s, instruments=names)
                break
            write_status(state="error", error=repr(e)[:200], errors_in_row=errors, positions=plist, spots=spots,
                         risk=risk.s, instruments=names)
            time.sleep(10)


if __name__ == "__main__":
    main()
