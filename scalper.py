"""
Scalping bot — the main bot's rules, faster and with fixed point exits.

Same as bot.py : strategy.signal_v2 entry rules (EMA cross with trend, RSI, confirmation candle,
                 15-min-style higher-timeframe filter), in-the-money options at least ITM_STRIKES
                 full strikes deep, crude/news agent vetoes and event-day block, paper/live modes,
                 limit orders, kill switch.
Different      : SCALP_CANDLE_MINUTES candles (1 min by default) so signals come more often;
                 exits are fixed points — stop SCALP_SL_POINTS, target SCALP_TARGET_POINTS — measured
                 on the option premium (default) or the index (SCALP_POINTS_ON); at +SCALP_BE_AT_POINTS
                 the stop moves to cost; time stop SCALP_MAX_HOLD_MIN; its own daily limits, state,
                 log and trade files (scalp_*.json/csv/log).
Live mode      : the stop sits at the exchange as an SL order from the moment of entry (moved to cost
                 at +5), so a 5-point stop is enforced even if this program stops.

Run from the control panel (Scalper page), or:  python scalper.py
"""
import csv, json, os, sys, time
from datetime import date

import config
import strategy
import bot
from scalp_lab.live import EXPERIMENTAL, LABELS, LabLive
from bot import TERMINAL, Broker, Candles, acquire_lock, hm, now, tick_round
from agents import load_cache, market

STATE_FILE = "scalp_state.json"
TRADES_FILE = "scalp_trades.csv"
STATUS_FILE = "scalp_status.json"
LOG_FILE = "scalp.log"
STOP_FILE = "STOP_SCALP"
POLL = 2


def log(*a):
    msg = f"[{now():%H:%M:%S}] " + " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{now():%Y-%m-%d} {msg}\n")


bot.log = log  # Broker messages go to the scalper's log


class ScalpRisk:
    """Daily limits for the scalper only; survive restarts."""

    def __init__(self):
        self.s = {"date": str(date.today()), "pnl": 0.0, "trades": 0, "consec_losses": 0, "locked": False, "reason": ""}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if saved.get("date") == self.s["date"]:
                self.s = saved

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.s, f, indent=2)

    def record(self, pnl):
        s = self.s
        s["pnl"] += pnl; s["trades"] += 1
        s["consec_losses"] = s["consec_losses"] + 1 if pnl < 0 else 0
        if s["pnl"] <= -config.SCALP_MAX_DAILY_LOSS:
            self.lock(f"scalp daily loss limit hit (₹{s['pnl']:.0f})")
        elif s["consec_losses"] >= config.SCALP_MAX_CONSECUTIVE_LOSSES:
            self.lock(f"{s['consec_losses']} scalp losses in a row")
        elif s["trades"] >= config.SCALP_MAX_TRADES_PER_DAY:
            self.lock("max scalps for the day reached")
        self.save()

    def lock(self, reason):
        self.s["locked"], self.s["reason"] = True, reason
        log("SCALP RISK LOCK:", reason, "- no more scalps today.")
        self.save()

    def room(self):
        return config.SCALP_MAX_DAILY_LOSS + self.s["pnl"]


def signal_cfg():
    """The main strategy's settings, adjusted for fast candles."""
    c = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    c["MIN_ATR_PCT"] = config.SCALP_MIN_ATR_PCT
    c["RR"] = config.SCALP_TARGET_POINTS / max(config.SCALP_SL_POINTS, 0.01)
    return c


def write_status(**kw):
    kw.update(updated=now().strftime("%Y-%m-%d %H:%M:%S"), mode=config.MODE)
    with open(STATUS_FILE + ".tmp", "w") as f:
        json.dump(kw, f, default=str)
    os.replace(STATUS_FILE + ".tmp", STATUS_FILE)


def write_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, bot.TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def main_bot_holds(index_name):
    """True if the main bot currently holds a position on this index (from its fresh status file)."""
    try:
        with open("status.json") as f:
            st = json.load(f)
        return any(p.get("index") == index_name for p in st.get("positions", []))
    except Exception:
        return False


def warmup(kite, spec, candles):
    m = config.SCALP_CANDLE_MINUTES
    try:
        data = kite.historical_data(spec["token"], now() - bot.timedelta(days=4), now(),
                                    "minute" if m == 1 else f"{m}minute")
        cur = candles.bucket(now())
        candles.done = [dict(o=c["open"], h=c["high"], l=c["low"], c=c["close"], t=c["date"].astimezone(bot.IST)) for c in data
                        if c["date"].astimezone(bot.IST) < cur][-400:]
        log(f"{spec['name']}: warm-up loaded {len(candles.done)} {m}-min candles.")
    except Exception as e:
        log(f"{spec['name']}: no historical data ({e}). Building live; first signal in ~{(config.EMA_TREND + 10) * m} min.")


def attach_volume(done, m):
    """Volume strategy: indices have no volume, so put SCALP_VOL_SYMBOL's (NIFTYBEES) 1-min volume, summed
    into m-min buckets, on each candle as "v", plus "day" for session VWAP. Candles Yahoo hasn't
    published yet keep v=None, and signal_volume skips them rather than guess."""
    try:
        d = market.chart(config.SCALP_VOL_SYMBOL, "1m", "5d", max_age=15)
    except Exception as e:
        log(f"Volume data unavailable ({str(e)[:80]}) — no volume signals this candle."); return
    vol = {}
    for ts, v in zip(d["t"], d["v"]):
        x = bot.datetime.fromtimestamp(ts, bot.IST)
        k = (x.date(), (x.hour * 60 + x.minute) // m * m)
        vol[k] = vol.get(k, 0) + (v or 0)
    for c in done[-200:]:
        t = c.get("t")
        if t is not None:
            t = t.astimezone(bot.IST)
            c["day"], c["v"] = t.date(), vol.get((t.date(), t.hour * 60 + t.minute))


class V2Live:
    """Scalper V2 entries (scalp_v2.py) for the live/paper loop: confirming-index candles, India VIX, option
    quotes (spread/volume/OI → liquidity filter + delta-based strike), agents as filters, structural cooldown,
    adaptive side confidence, and a CSV of every rejected setup with its score breakdown."""
    CROSS = {"NIFTY": ("NSE:NIFTY BANK", 260105), "SENSEX": ("NSE:NIFTY 50", 256265)}
    REJ_FILE = "scalp_rejections.csv"

    def __init__(self, kite, broker, names):
        import scalp_v2
        self.V, self.kite, self.broker = scalp_v2, kite, broker
        self.cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
        self.other = {}
        for n in names:
            key, token = self.CROSS[n]
            if key not in self.other:
                c = Candles(config.SCALP_CANDLE_MINUTES)
                warmup(kite, dict(token=token, name=key), c)
                self.other[key] = c
        self.vix, self.vix_day = None, None
        self.last_loss, self.reset = {}, {}
        self.completed = self._history()

    def _history(self):
        """(side, pnl) of this mode's completed scalps, oldest first — only closed trades, so no look-ahead."""
        if not os.path.exists(TRADES_FILE):
            return []
        with open(TRADES_FILE) as f:
            return [(r["side"], float(r["pnl"] or 0)) for r in csv.DictReader(f) if r.get("mode") == config.MODE]

    def poll(self, t):
        for key, c in self.other.items():
            try:
                c.update(t, self.broker.ltp(key))
            except Exception as e:
                log(f"{key}: no price ({str(e)[:60]})")

    def closed(self, n, side, pnl, t):
        self.completed.append((side, pnl))
        if pnl < 0:
            self.last_loss[(n, side)], self.reset[(n, side)] = t, False

    def _vix(self):
        if self.vix_day != date.today():
            try:
                d = market.chart("^INDIAVIX", "1d", "3mo", max_age=3600)
                self.vix = {bot.datetime.fromtimestamp(ts, bot.IST).date(): c for ts, c in zip(d["t"], d["c"]) if c}
                self.vix_day = date.today()
            except Exception as e:
                log(f"India VIX unavailable ({str(e)[:60]}) — VIX filter skipped.")
        return self.vix

    @staticmethod
    def _frame(done):
        import pandas as pd
        rows = [c for c in done if c.get("t") is not None]
        if not rows:
            return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
        idx = pd.DatetimeIndex([c["t"] for c in rows])
        return pd.DataFrame(dict(o=[c["o"] for c in rows], h=[c["h"] for c in rows], l=[c["l"] for c in rows],
                                 c=[c["c"] for c in rows], v=[c.get("v") for c in rows]), index=idx).astype(float)

    def reject(self, n, t, side, e, why):
        log(f"{n}: {side or '-'} setup rejected — {why} | score {e.get('score', 0):.0f} "
            f"[" + " ".join(f"{k[:4]} {e.get('s_' + k, 0):.0f}" for k in self.V.WEIGHTS) + f"] regime {e.get('regime', '')}")
        new = not os.path.exists(self.REJ_FILE)
        with open(self.REJ_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "time", "mode", "index", "signal", "score", "regime", "reason"] + ["s_" + k for k in self.V.WEIGHTS])
            w.writerow([date.today(), f"{t:%H:%M}", config.MODE, n, side, round(e.get("score", 0)), e.get("regime", ""), why] +
                       [e.get("s_" + k, "") for k in self.V.WEIGHTS])

    def _options(self, spec, side, spot, iv):
        """Nearest-expiry ATM..3-ITM strikes with quotes and model delta."""
        self.broker.option_for(spec, side, spot)                      # loads the instrument list once
        ex, step = spec["exchange"], spec["step"]
        insts = [i for i in self.broker._instruments[ex] if i["instrument_type"] == side and i["expiry"] >= date.today()]
        if not insts:
            return [], None
        exp = min(i["expiry"] for i in insts)
        atm = round(spot / step) * step
        want = {(atm - k * step if side == "CE" else atm + k * step): k for k in range(4)}
        chosen = {float(i["strike"]): i for i in insts if i["expiry"] == exp and float(i["strike"]) in want}
        keys = {f"{ex}:{i['tradingsymbol']}": i for i in chosen.values()}
        try:
            q = self.kite.quote(list(keys))
        except Exception:
            q = {k: v for k, v in self.kite.ltp(list(keys)).items()}
        T = max((bot.datetime.combine(exp, bot.datetime.min.time().replace(hour=15, minute=30), tzinfo=bot.IST) - now())
                .total_seconds(), 60) / (365 * 24 * 3600)
        out = []
        for k, i in keys.items():
            d = q.get(k) or {}
            depth = d.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0].get("price") or None
            ask = (depth.get("sell") or [{}])[0].get("price") or None
            out.append(dict(strike=float(i["strike"]), premium=float(d.get("last_price") or 0), bid=bid, ask=ask,
                            volume=d.get("volume"), oi=d.get("oi"), delta=self.V.bs(spot, float(i["strike"]), T, iv, side)[1],
                            itm_steps=want[float(i["strike"])], inst=i, key=k))
        return out, exp

    def entry(self, n, spec, done, spot, t, crude, news, room, glob=None):
        """Returns a trade plan dict, or None (with the reason logged)."""
        import pandas as pd
        V, cfg = self.V, self.cfg
        attach_volume(done, config.SCALP_CANDLE_MINUTES)
        key = self.CROSS[n][0]
        other = self._frame(self.other[key].done) if key in self.other else None
        vix = self._vix()
        F = V.features(self._frame(done[-300:]), cfg, pd.Series(vix) if vix else None, other)
        if len(F) < 60:
            return None
        e = V.evaluate(F.tail(3), cfg).iloc[-1].to_dict()
        fr = F.iloc[-1]
        for sd, sgn in (("CE", 1), ("PE", -1)):                     # a fresh setup after a stop needs a reset
            if (n, sd) in self.last_loss and (fr.c - fr.ema9) * sgn < 0:
                self.reset[(n, sd)] = True
        side = e["side"]
        if not e["trigger_side"]:
            return None
        if not side:
            return self.reject(n, t, e["trigger_side"], e, e["reason"])
        if cfg.get("SCALP_STRUCTURAL_COOLDOWN", True) and (n, side) in self.last_loss:
            lb = max(V.P(cfg, "SCALP_PULLBACK_LOOKBACK"), V.P(cfg, "SCALP_BREAKOUT_LOOKBACK"))
            if not self.reset.get((n, side)) or (t - self.last_loss[(n, side)]).total_seconds() < (lb + 1) * 60:
                return self.reject(n, t, side, e, "structural cooldown (no new setup since the stop)")
        veto = strategy.agent_veto(side, crude, news, config, glob)  # macro / event risk: hard filters
        if veto:
            return self.reject(n, t, side, e, veto)
        adj = 0
        if config.USE_TRADER_AGENT:                                 # debate: confidence adjustment only
            try:
                from agents import debate, pipeline, trader
                dec = trader.decide(dict(side=side, why=f"V2 {e['entry_type']} {e['regime']}"),
                                    debate.run(pipeline.reports(config), side), config)
                adj = -15 if dec["action"] == "HOLD" else 0
            except Exception as ex:
                log(f"trader agent unavailable ({str(ex)[:60]})")
        iv = (list(vix.values())[-1] / 100) if vix else 0.13
        cands, exp = self._options(spec, side, spot, iv)
        lot = cands[0]["inst"]["lot_size"] if cands else 0
        qty = lot * config.SCALP_LOTS
        if not qty:
            return self.reject(n, t, side, e, "no option chain")
        best, liq, why = V.score_options(cands, cfg, config.MAX_PREMIUM_PER_TRADE / qty)
        if not best:
            return self.reject(n, t, side, e, "option filter: " + why)
        final = e["score"] - V.WEIGHTS["liquidity"] + liq + adj
        pen = V.side_penalty(self.completed, side, cfg)
        e = dict(e, score=final, s_liquidity=liq)
        if final < cfg["TRADE_SCORE_MIN"] + pen:
            return self.reject(n, t, side, e, f"score after liquidity/agents {final:.0f} < {cfg['TRADE_SCORE_MIN'] + pen}"
                                              + (f" ({side} side penalty +{pen})" if pen else ""))
        sl = V.premium_stop(fr.atr, best["delta"], cfg)
        tg = cfg.get("SCALP_RR", 2.0) * sl
        be = cfg.get("SCALP_BE_AT_R", 1.2) * sl if cfg.get("SCALP_BE_AT_R") else 0
        if qty * (sl + strategy.cost_per_unit(best["premium"], qty)) > room:
            return self.reject(n, t, side, e, "loss at stop exceeds today's remaining risk")
        sp = f"spread {best['spread_pct']:.2f}%" if best.get("spread_pct") is not None else "spread n/a"
        why = (f"V2 {e['entry_type']} {e['regime']} score {final:.0f} | delta {abs(best['delta']):.2f} {sp} | "
               f"SL {sl:.1f} / TGT {tg:.1f} prem pts")
        return dict(sig=dict(side=side, why=why), inst=best["inst"], key=best["key"], px=best["premium"], qty=qty,
                    sl=sl, tg=tg, be=be)


def main():
    live = config.MODE == "live"
    if not acquire_lock("scalp.pid", "scalper.py"):
        log("Another copy of the scalper is already running — not starting a second one.")
        sys.exit(1)
    bot.keep_awake()
    if live and config.SCALP_STRATEGY in EXPERIMENTAL:
        log(f"'{config.SCALP_STRATEGY}' is an experimental strategy — paper only. Not starting in live mode.")
        sys.exit(1)
    if live and not config.SCALP_ALLOW_LIVE:
        log("Live scalping is switched off (Settings → Scalper → Allow live scalping). Not starting.")
        sys.exit(1)
    if live:
        ok = os.environ.get("BOT_CONFIRM_LIVE") or input("LIVE SCALPING - real money. Type YES to continue: ")
        if ok.strip() != "YES":
            sys.exit("Aborted.")
    if hm(config.SCALP_NO_ENTRY_AFTER) >= hm(config.SQUARE_OFF_AT):
        config.SCALP_NO_ENTRY_AFTER = "14:30"
        log("Scalp no-entry time was at/after square-off — using 14:30.")
    if not live and config.PAPER_FEED == "free":
        from feed import FreeFeed
        kite = FreeFeed()
        log("Paper feed: FREE — Yahoo index prices lag ~1 min and option prices are modelled. "
            "Good for testing the app; for realistic 5-point scalps use the Zerodha feed.")
    else:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=config.API_KEY)
        with open("access_token.txt") as f:
            kite.set_access_token(f.read().strip())
    broker, risk = Broker(kite, live), ScalpRisk()
    broker.tag = "kitescalp"   # shows which orders the scalper placed on the Zerodha page
    names = config.instruments()
    specs = {n: config.INSTRUMENT_SPECS[n] for n in names}
    candles = {n: Candles(config.SCALP_CANDLE_MINUTES) for n in names}
    scfg = signal_cfg()
    on_prem = config.SCALP_POINTS_ON == "premium"
    sl_pts, tg_pts, be_pts = config.SCALP_SL_POINTS, config.SCALP_TARGET_POINTS, config.SCALP_BE_AT_POINTS
    use_vol = config.SCALP_STRATEGY == "volume"
    v2 = config.SCALP_STRATEGY == "v2"
    lab = config.SCALP_STRATEGY in EXPERIMENTAL
    log(f"Scalper started in {config.MODE.upper()} on {', '.join(names)} | {config.SCALP_CANDLE_MINUTES}-min candles | "
        f"entry rule: {LABELS[config.SCALP_STRATEGY] + ' (EXPERIMENTAL, paper only)' if lab else 'Scalper V2 (filtered, dynamic premium stop)' if v2 else 'volume spike (' + config.SCALP_VOL_SYMBOL + ' volume)' if use_vol else 'EMA cross'} | "
        f"SL {sl_pts:g} / target {tg_pts:g} {'premium' if on_prem else 'index'} pts | ITM {config.ITM_STRIKES} | "
        f"{config.SCALP_LOTS} lot(s)")
    for n in names:
        warmup(kite, specs[n], candles[n])
    v2 = V2Live(kite, broker, names) if v2 else None
    labs = {n: LabLive(config.SCALP_STRATEGY) for n in names} if lab else {}
    for n in labs:
        labs[n].feed(candles[n].done)      # warm the lab features up on history (signals from it are ignored)
    lab_sig = {}

    pos, spots, last_exit = {}, {}, {}

    def close(n, reason, px_hint=None):
        p = pos[n]
        px = px_hint if px_hint is not None else broker.ltp(p["key"])
        f = broker.sell(p["exchange"], p["sym"], p["qty"], px, p.get("sl_id"))
        if f.status != "STOP_STILL_OPEN":
            p["sl_id"] = None
        if f.qty:  # a partial exit keeps the rest open; the loop retries it
            p["sold"] = p.get("sold", 0) + f.qty
            p["sold_value"] = p.get("sold_value", 0.0) + f.qty * f.price
            p["qty"] -= f.qty
        if p["qty"] > 0:
            risk.lock(f"scalp exit incomplete ({p['qty']} left) - manual check needed"); return
        p["qty"] = p["sold"]
        exit_px = p["sold_value"] / p["sold"]
        pnl = (exit_px - p["entry"]) * p["qty"]
        log(f"EXIT {p['sym']} @ {exit_px:.2f} ({reason}) P&L ₹{pnl:.0f} | held {(now() - p['t']).seconds // 60} min")
        write_trade(dict(date=date.today(), mode=config.MODE, symbol=p["sym"], qty=p["qty"],
                         entry_time=f"{p['t']:%H:%M}", entry=p["entry"], exit_time=f"{now():%H:%M}", exit=exit_px,
                         pnl=round(pnl, 2), reason=reason, index=n, side=p["side"], idx_entry=round(p["idx_entry"], 2),
                         idx_sl=p["sl_desc"], idx_target=p["tg_desc"], idx_exit=round(spots.get(n, 0), 2), why=p["why"]))
        risk.record(pnl)
        last_exit[n] = now()
        if v2:
            v2.closed(n, p["side"], pnl, now())
        del pos[n]

    while True:
        try:
            t = now()
            if os.path.exists(STOP_FILE):
                log("Stop requested.")
                for n in list(pos):
                    close(n, "kill switch")
                break
            if t.weekday() >= 5 or t < hm("09:15"):
                write_status(state="waiting for market open", positions=[], risk=risk.s, instruments=names)
                time.sleep(30); continue
            if t >= hm(config.SQUARE_OFF_AT):
                for n in list(pos):
                    close(n, "square-off time")
                log(f"Scalping day done. P&L ₹{risk.s['pnl']:.0f}, trades {risk.s['trades']}.")
                write_status(state="day finished", positions=[], risk=risk.s, instruments=names)
                break
            agents = load_cache()
            crude = agents.get("crude") if config.USE_CRUDE_AGENT else None
            news = agents.get("news") if config.USE_NEWS_AGENT else None
            glob = agents.get("global") if config.USE_GLOBAL_AGENT else None
            if v2:
                v2.poll(t)
            for n in names:
                spec = specs[n]
                spot = broker.ltp(spec["index"])
                spots[n] = round(spot, 2)
                new_candle = candles[n].update(t, spot)

                # ---- manage the open scalp
                if n in pos:
                    p = pos[n]
                    sl_fill = broker.sl_filled(p.get("sl_id"))
                    if sl_fill:
                        sl_px = sl_fill.price
                        pnl = (sl_px - p["entry"]) * p["qty"]
                        log(f"Exchange stop filled @ {sl_px:.2f} P&L ₹{pnl:.0f}")
                        p["sl_id"] = None
                        write_trade(dict(date=date.today(), mode=config.MODE, symbol=p["sym"], qty=p["qty"],
                                         entry_time=f"{p['t']:%H:%M}", entry=p["entry"], exit_time=f"{t:%H:%M}",
                                         exit=sl_px, pnl=round(pnl, 2), reason="stop (exchange)", index=n, side=p["side"],
                                         idx_entry=round(p["idx_entry"], 2), idx_sl=p["sl_desc"], idx_target=p["tg_desc"],
                                         idx_exit=spot, why=p["why"]))
                        risk.record(pnl); last_exit[n] = t
                        if v2:
                            v2.closed(n, p["side"], pnl, t)
                        del pos[n]
                        continue
                    opt = broker.ltp(p["key"])
                    p["ltp"] = opt
                    if p.get("on_prem", on_prem):
                        level, up = opt, True              # long option: premium always 'up' is good
                    else:
                        level, up = spot, p["side"] == "CE"
                    d = 1 if up else -1
                    if (level - p["stop"]) * d <= 0:
                        close(n, "stop at cost" if p["be_done"] else "stop-loss", opt)
                    elif (level - p["target"]) * d >= 0:
                        close(n, f"target {p.get('tg_pts', tg_pts):g} pts", opt)
                    else:
                        pbe = p.get("be_pts", be_pts)
                        if pbe and not p["be_done"] and (level - p["ref"]) * d >= pbe:
                            cost = p["cost_unit"] if p.get("on_prem", on_prem) else strategy.cost_points(p["entry"], p["qty"])
                            p["stop"] = p["ref"] + d * cost
                            p["be_done"] = True
                            log(f"{n}: +{pbe:g} pts — stop moved to cost")
                            broker.move_stop(p.get("sl_id"), p["entry"] + p["cost_unit"])
                        held = (t - p["t"]).total_seconds() / 60
                        if config.SCALP_MAX_HOLD_MIN and held >= config.SCALP_MAX_HOLD_MIN:
                            close(n, f"time stop {config.SCALP_MAX_HOLD_MIN:g} min", opt)

                if lab and new_candle:          # the lab detector sees every candle, even while a scalp is open
                    attach_volume(candles[n].done, config.SCALP_CANDLE_MINUTES)
                    lab_sig[n] = labs[n].feed(candles[n].done)

                # ---- new scalps on candle close
                if not new_candle or n in pos or risk.s["locked"]:
                    continue
                if not (hm(config.SCALP_NO_ENTRY_BEFORE) <= t <= hm(config.SCALP_NO_ENTRY_AFTER)):
                    continue
                if n in last_exit and (t - last_exit[n]).total_seconds() < config.SCALP_COOLDOWN_MIN * 60:
                    continue
                if pos and len(pos) >= config.MAX_OPEN_POSITIONS:
                    continue
                if config.SCALP_AVOID_OVERLAP and main_bot_holds(n):
                    continue
                if v2:
                    plan = v2.entry(n, spec, candles[n].done, spot, t, crude, news, risk.room(), glob)
                    if not plan:
                        continue
                    sig, inst, key, px, qty = plan["sig"], plan["inst"], plan["key"], plan["px"], plan["qty"]
                    t_sl, t_tg, t_be, t_prem = plan["sl"], plan["tg"], plan["be"], True
                else:
                    if lab:
                        sig = lab_sig.pop(n, None)
                    else:
                        if use_vol:
                            attach_volume(candles[n].done, config.SCALP_CANDLE_MINUTES)
                        sig = strategy.scalp_signal(candles[n].done, scfg)
                    if not sig:
                        continue
                    veto = strategy.agent_veto(sig["side"], crude, news, config, glob)
                    if veto:
                        log(f"{n}: {sig['side']} scalp skipped — {veto}"); continue
                    inst = broker.option_for(spec, sig["side"], spot)
                    if not inst:
                        log(f"{n}: no ITM option for {sig['side']}"); continue
                    key = f"{spec['exchange']}:{inst['tradingsymbol']}"
                    px = broker.ltp(key)
                    qty = inst["lot_size"] * config.SCALP_LOTS
                    t_sl, t_tg, t_be, t_prem = sl_pts, tg_pts, be_pts, on_prem
                if px * qty > config.MAX_PREMIUM_PER_TRADE:
                    log(f"{n}: skip — premium ₹{px * qty:,.0f} above cap"); continue
                cost_unit = strategy.cost_per_unit(px, qty)
                worst = qty * ((t_sl if t_prem else t_sl * 0.7) + cost_unit)
                if worst > risk.room():
                    log(f"{n}: skip — loss at stop ≈₹{worst:,.0f} > ₹{max(risk.room(), 0):,.0f} left today"); continue
                bought = broker.buy(spec["exchange"], inst["tradingsymbol"], qty, px)
                if bought.status not in TERMINAL:
                    risk.lock(f"buy order {bought.oid} still open after cancel - check Kite")
                if not bought:
                    continue
                fill, qty = bought.price, bought.qty
                if t_prem:
                    ref, stop, target = fill, fill - t_sl, fill + t_tg
                    sl_desc, tg_desc = f"opt {stop:.2f}", f"opt {target:.2f}"
                    exch_trigger = stop
                else:
                    d = 1 if sig["side"] == "CE" else -1
                    ref, stop, target = spot, spot - d * t_sl, spot + d * t_tg
                    sl_desc, tg_desc = round(stop, 2), round(target, 2)
                    exch_trigger = fill - 2 * t_sl  # backstop only; the bot exits on the index level first
                sl_id = None
                if live and exch_trigger > 0.5:
                    try:
                        sl_id = broker._order(spec["exchange"], inst["tradingsymbol"], "SELL", qty, order_type="SL",
                                              trigger_price=tick_round(exch_trigger), price=tick_round(exch_trigger * 0.97))
                        broker._stop_trigger[sl_id] = tick_round(exch_trigger)
                    except Exception as e:
                        log("Could not place the exchange stop — exiting for safety:", e)
                        pos[n] = dict(sym=inst["tradingsymbol"], key=key, exchange=spec["exchange"], qty=qty, entry=fill,
                                      side=sig["side"], t=t, idx_entry=spot, why=sig["why"], sl_desc="", tg_desc="")
                        close(n, "no exchange stop"); continue
                pos[n] = dict(sym=inst["tradingsymbol"], key=key, exchange=spec["exchange"], qty=qty, entry=fill,
                              side=sig["side"], t=t, idx_entry=spot, ref=ref, stop=stop, target=target, be_done=False,
                              cost_unit=cost_unit, sl_id=sl_id, why=sig["why"], sl_desc=sl_desc, tg_desc=tg_desc, ltp=fill,
                              on_prem=t_prem, tg_pts=t_tg, be_pts=t_be)
                log(f"SCALP {sig['side']} {inst['tradingsymbol']} x{qty} @ {fill:.2f} | {n} {spot:.1f} | "
                    f"SL {sl_desc} TGT {tg_desc} | {sig['why']}")

            write_status(state="running", instruments=names, risk=risk.s, spots=spots,
                         lab={n: labs[n].status() for n in labs},
                         candles={n: len(candles[n].done) for n in names},
                         positions=[dict(index=n, symbol=p["sym"], side=p["side"], qty=p["qty"], entry=round(p["entry"], 2),
                                         ltp=p.get("ltp"), pnl=round((p.get("ltp", p["entry"]) - p["entry"]) * p["qty"], 2),
                                         stop=p["sl_desc"] if not p["be_done"] else "at cost", target=p["tg_desc"],
                                         since=p["t"].strftime("%H:%M")) for n, p in pos.items()])
            time.sleep(POLL)
        except KeyboardInterrupt:
            log("Ctrl+C - stopping.")
            if pos:
                log("!!! Open scalps", list(pos), "- exit manually in Kite !!!")
            break
        except Exception as e:
            log("Error:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
