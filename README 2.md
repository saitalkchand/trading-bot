# Kite Bot — Nifty options bot with a control panel

## Start (one click)
Double-click **Start Kite Bot.command**.
The first run installs everything (1–2 min). After that, the control panel opens in your browser at http://127.0.0.1:8765.
Keep the Terminal window open while the bot runs. Close it, or use **Quit app**, to shut down.

If macOS blocks it ("unidentified developer"): right-click the file → **Open** → **Open**. You only need to do this once.

## Pages
- **Dashboard**: Start/Stop, Paper/Live switch, today's P&L, trades used, daily loss-limit bar, open position, live activity log
- **Login**: daily Zerodha 2FA login (SEBI requires it every trading day)
- **Settings**: API key/secret, lots, strategy, stop-loss/target, risk limits, timings
- **Trade log**: every trade, filterable by paper/live
- **P&L**: net P&L, win rate, drawdown, best/worst day, daily chart, monthly and daily tables
- **Backtest**: run the strategy on the last ≤55 days of real Nifty data

## Strategy v2 (strategy.py — the bot and the backtest share it)
- **What:** Nifty, Sensex or both; buys **in-the-money** options, at least ITM_STRIKES full strikes deep (Nifty ≥ 50 pts, Sensex ≥ 100 pts per strike), nearest weekly expiry.
- **Entry:** fresh EMA 9/21 cross, in the direction of the EMA-50 trend **and the 15-min EMA-20 trend**, RSI momentum but not exhausted, a confirmation candle, and enough volatility (ATR). Optional ADX filter (off).
- **Risk : reward 1:2 on the index:** stop at the recent swing (0.6–1.5 × ATR), target = 2 × risk.
- **Stop to cost at 1:1:** once the trade is up 1 × risk, the stop moves to *cost* — entry plus the points needed to pay charges and slippage — so the trade can't become a loss. In live mode the exchange-side stop is raised to cost too.
- Optional (off by default, didn't help consistently in testing): trailing stop, time stop, cost stop on the option premium.
- **No entries** after 13:30. Square-off at 15:10 (never later than 15:20 — Zerodha auto-squares F&O at 15:26; the bot corrects unsafe times). Max 1 open position across both indices.

**Backtest, last 55 days, Nifty + Sensex, 1 lot:** +₹8,215 net, 36 trades, 42% wins, max drawdown −₹4,338.
That's too few trades to prove an edge, and Nifty lost money in the first half of the period. Paper-trade first.

## Scalper (scalper.py, Scalper page)
A second bot with the **same entry rules, agents and ITM options**, on 1-minute candles, with fixed exits:
**5-point stop, 10-point target** on the option premium (or the index — Settings → Scalper), stop to cost at +5,
10-minute time stop, 2-minute cooldown, its own limits (10 scalps, ₹1,500 loss checked before each entry,
3 losses in a row) and its own files (`scalp_trades.csv`, `scalp.log`, `scalp_state.json`).
It runs alongside the main bot or alone, and skips an index while the main bot holds a position there.
In live mode the 5-point stop sits at the exchange as an SL order from the moment of entry.

**Backtest (last ~17 days of 1-minute data, 1 lot):** profitable only with perfect fills (+₹1,549);
with ≈1 point of slippage/spread it lost ₹9,911 (PF 0.43) — a 5-point option stop is inside normal 1-minute noise.
So live scalping is **off** until you tick *Allow live scalping*. Paper-trade it first, ideally on the Zerodha feed
(the free feed lags ~1 minute, which is too slow for 5-point exits). `python scalp_backtest.py` re-runs the test.

### Scalper V2 (default entry rule; `scalp_v2.py`)
Same scalper loop, new entry pipeline shared with its backtest: regime (only strong trends) → EMA9/21 alignment with
3-candle slopes → VWAP side and ≤0.12% distance → pullback-to-EMA entry or a volume breakout confirmed one candle later →
wick/extension checks → opening-range, India VIX percentile and other-index (Bank Nifty for Nifty, Nifty for Sensex)
filters → trade-quality score (trend 20, momentum 15, VWAP 10, RSI 10, MACD 10, volume 10, breakout 10, option liquidity
10, volatility 5) ≥ 75. The option is picked by delta (0.55–0.70) and, on the Zerodha feed, rejected if its spread is
over 1% or its volume/OI is thin vs nearby strikes. Premium stop = 1-min ATR × delta × 0.8 (5–12 pts), target 2×,
stop to cost at 1.2R. After a stop the same side needs a fresh setup; a side whose recent scalps lose needs +10 score.
Limits: 6 scalps, 2 losses in a row, ₹1,000/day, 5-min cooldown, no entry before 09:35. Agents only filter
(crude/news veto, debate −15 score); none can override a limit. Every rejected setup is logged with its score
breakdown to `scalp_rejections.csv`. `SCALP_V2_STRICT` makes every V2 rule mandatory.

**Backtest (`python scalp_v2_backtest.py` → `reports/scalper_v2/`, 18 days, both indices, identical engine):**
current scalper 95 trades, −₹11,099, PF 0.41 (base fills); V2 9 trades, −₹1,723, PF 0.27 (base), −₹1,413 (conservative).
V2 loses far less only because it trades ~10× less — its expectancy per trade (−0.63R) is no better, and 9 trades
prove nothing. Walk-forward (2 folds, 6 out-of-sample days): 4 V2 trades. **V2 has not passed; live scalping stays off.**

## Scalper settings (from 15 Oct 2026)
15-point stop, 30-point target on the option premium, stop to cost at +15, for every scalper rule (V2's volatility stop is
off, so V2 uses the same 15 / 2×15). Scalper daily loss limit ₹2,500 (≈2 full stops on 1 lot: 15 × 65 = ₹975 + charges),
time stop 30 minutes. Backtest on the frozen 19-day set: V2 +₹2,879 (11 trades, base fills) / +₹3,423 (10, conservative);
the volume rule −₹18,694. Too few trades to prove anything — paper first. Old values are in `backups/settings.before-15-30.json`.

## Strategy lab (scalp_lab/, Strategy lab page) — research, paper only
`python -m scalp_lab.experiments` (or **Re-run lab** on the Strategy lab page, ~3 min) compares scalper variants on one frozen dataset
(`scalp_lab/data/snapshot_2026-09-16_2026-10-14.pkl`, 19 trading days, NIFTY, 1 lot) with one engine and cost model → `reports/scalp_lab/`.
- **Baseline reproduced:** the lab engine re-creates `scalp_backtest.py`'s volume-rule trades exactly (75 base / 38 conservative).
- **New rules:** liquidity-sweep reversal and breakout-retest (`scalp_lab/strategies.py`), a regime/no-trade filter (`regime.py`),
  exit policies with an audit trail and a never-widen stop rule (`exits.py`), risk-based sizing that never scales up (`sizing.py`).
- **Result: nothing is profitable after realistic costs.** Baseline −₹8,927 (69 trades, base fills, stricter lab execution).
  ~70% of its losers are false breakouts, and slippage + charges (~₹166/trade) equal ~0.5R per trade. Sweep (7 trades) and
  retest (5 trades) are too few to judge and negative under both fill models and walk-forward. NIFTYBEES relative volume shows
  no directional information for NIFTY (correlation −0.01).
- **Paper-trading a new rule:** Settings → Scalper → Entry rule → *EXPERIMENTAL · …*, then Start paper scalper. The Scalper page
  shows regime, proxy VWAP and each setup (provisional / confirmed / rejected and why). These rules refuse live mode.
- To extend the dataset later: `python -m scalp_lab.data` freezes the current 1-min cache as a new snapshot.

## Agent team (agents/)
Every strategy signal goes through: **analysts → bull/bear debate → trader → risk manager → order**.
Agents can only **stop** a trade, never start one. The **Agent team** page shows every report, the debate, and recent decisions.

| Agent | What it reads | Role |
|---|---|---|
| **Technical** | RSI, MACD, Bollinger Bands, EMA-50 trend on 5-min index candles | feeds the debate |
| **News** | Google News, last 24 h: market, macro data (GDP/CPI/PMI…), heavyweight announcements; flags RBI/Fed/Budget/election days | veto on strong sentiment + feeds the debate |
| **Crude oil** | Brent % change vs previous close | Brent up ≥1% blocks CE, down ≥1% blocks PE |
| **Sentiment** | Reddit (r/IndianStreetBets, r/IndianStockMarket, r/IndiaInvestments), India VIX, heavyweight breadth | feeds the debate |
| **Fundamental** | earnings/revenue growth, P/E, debt, analyst views for 12 Nifty heavyweights; results days (refreshes every 12 h) | feeds the debate |
| **Debate** | bull researcher vs bear researcher, weighted by analyst trust; flags event days and "only the chart agrees" | margin −1…+1 |
| **Trader** | signal + debate → BUY CE / BUY PE / HOLD | HOLDs when the debate is ≥ `TRADER_BLOCK_MARGIN` against the signal |
| **Risk manager** | hard stop, daily lock, open positions, premium cap, estimated loss vs loss budget left, crude/news/event vetoes | final approval |

Yahoo data goes through `agents/market.py`, which uses yfinance (Yahoo rejects plain requests with HTTP 429), backs off after rate limits, and reuses the last good reading for up to 30 min.
The backtest still tests only the crude agent; the others need live data.

## One-time Zerodha setup
1. Create an app at https://developers.kite.trade (Kite Connect subscription).
2. Set **Redirect URL** = `http://127.0.0.1:8765/callback`
3. Whitelist your **static IP** in the app (required by SEBI for API orders).
4. Paste the API key and secret into **Settings**.

## Daily routine
Paper (free feed): open the app → **Start**. Live or Zerodha feed: **Zerodha** tab → log in → **Start**. The bot waits for 09:15, enters 09:30–13:30, and squares off at 15:10.

## Paper trading without keys
Settings → Paper trading → **Paper price feed = free** (the default). It uses live Nifty/Sensex prices from Yahoo (may lag about a minute) and modelled ITM option prices. No Zerodha API key or login is needed. Switch to `zerodha` for real option prices once you have Kite Connect.

## Safety
- Starts in **Paper** mode (live prices, simulated orders).
- **Live** asks you to type YES every time you start.
- Risk locks (daily loss, max trades, losses in a row) are saved, so restarting doesn't reset them.
- In live mode a stop-loss order sits at the exchange, so you're protected even if the app crashes.
- Stop always exits any open position first.

Paper trade for at least 20 days before going live. The strategy is an example, not a proven edge.

## Execution safety (Oct 2026)
- **Fills are what Zerodha filled.** A partly filled buy is tracked at the filled quantity; a partly filled exit keeps the rest open, locks new entries and keeps trying. An order whose request errored is looked up by its unique tag before anything is re-sent.
- **The exchange stop never leaves a position untracked.** The position is saved before the stop is placed; if placing it fails, the bot's own stop stays active and the exchange stop is retried every 30 s. If the stop can't be cancelled at exit, no second sell is sent (it could leave you short). The exchange stop only ever moves up.
- **Restarts resume.** Open positions are saved to `positions.json`. On a live restart the bot compares them with Zerodha's MIS positions: it follows Zerodha's quantity, books trades closed while it was off, and locks new entries if anything doesn't match or an unknown position is open.
- **One copy only.** `bot.pid` / `scalp.pid` stop a second copy starting, including after the control panel itself was restarted.
- **No trading on stale prices.** The free feed refuses a Yahoo bar older than 5 minutes during market hours instead of serving a cached price. After any price gap the bot skips entries until it has a clean candle. Errors show on the dashboard (`status.json` state `error`); a Zerodha permission or login error stops a flat bot with a clear message.
- **Net P&L.** Each trade records estimated `charges` and `net_pnl`; the daily loss limit and the P&L page use net.
- **Control panel only answers itself.** Requests from other hosts, other web pages, or non-JSON POSTs are refused.

## Tests
`./.venv/bin/pip install -r requirements-dev.txt` then `./.venv/bin/python -m pytest tests -q`.
Uses a scripted fake broker and synthetic prices — no orders, no network. `tests/golden/signal_v2.json` pins strategy v2's signals; regenerate with `python tests/test_strategy_regression.py --update` only for an intended strategy change.

## Files it creates
`settings.json` (your settings and keys — never share it), `trades.csv`, `bot.log`, `state.json`, `status.json`, `positions.json`, `bot.pid`, `access_token.txt`.

## Command-line (optional)
`python app.py` (control panel) · `python bot.py` (bot only) · `python backtest.py --days 30 --lots 2`

## Strategy V3 research (not live)
`v3/` holds a research backtester (regimes, quality score, walk-forward, Monte Carlo, cost scenarios).
Run `python -m v3.run_research`; results go to `reports/v3/` (start with `V3_REPORT.md`).
V3 did **not** pass out-of-sample testing, so the live bot still runs V2.
