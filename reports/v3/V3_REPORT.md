# Strategy V3 — research report

**Verdict: V3 did not pass out-of-sample testing. The live bot was not changed.**
V3 meets every target on the 55 days used to design it. In walk-forward testing it cut drawdown and the worst day, but made less money than V2, and the validation step rejected it. Most V3 ideas did not survive at all. Two risk controls are worth adopting regardless of edge (see "What to do now").

All numbers are at 4 lots, with "base" costs (charges + 0.25% slippage + 0.25% half-spread per order), modelled option prices (Black-Scholes on India VIX), and no look-ahead. Full tables are in `results.md`; code is in `v3/`.

---

## Data and how much it can tell us

| Set | What | Trades (V2 rules) | Role |
|---|---|---|---|
| 5-min, 55 days (23 Jul – 8 Oct 2026) | the strategy's real timeframe | 36 | main evidence — **small** |
| 1-hour, ~700 sessions (Dec 2023 – Oct 2026) | same rules on hourly bars | 62 | independent check of entry ideas only — 74% of its trades exit at square-off, so it says little about stops, targets or time of day |

Yahoo gives only ~60 days of 5-minute data. With 36 trades, a difference of a few trades swings every metric, so **nothing here is statistically proven**, positive or negative.

**Engine check:** the new engine reproduces `backtest.py` exactly (36 trades, ₹44,483 with V2's own cost model). V2 vs V3 differences come from the rules, not the code.

**Realistic costs matter:** with slippage and spread, V2 falls from ₹44,483 to ₹34,231. Under the "stress" scenario V2 loses ₹16,108.

---

## 1. What the 36 V2 trades show

| Pattern | Evidence | Holds elsewhere? |
|---|---|---|
| **Entries before 10:00 lose** | 09:15–10:00: 12 trades, −₹11,500, −0.16R. 11:00–13:00: 12 trades, +₹48,100, +0.91R | Holds in both halves of the 55 days. Not seen in the broader pool of setups, so it is specific to V2's signal. Likely cause: V2's EMAs carry over from the previous day, so opening gaps create false crossovers. |
| Losers had wider stops | 39.8 vs 30.5 points median | Information known at entry, but no stop rule exploited it (§5) |
| Many losers went +0.6–0.9R first | median MFE of losers 0.72R | Partial exits and trailing stops didn't help (§6) |
| "Textbook quality" points the wrong way | Winners had lower ADX, lower volume and more sideways votes; higher-TF-aligned trades lost −₹26k on 10 trades | The quality score built from these features has no predictive power (§2) |
| Event days lose | 6 trades on RBI/Fed decision days: −₹18,189, −0.55R | Plausible a priori; only 4 event days |
| Expiry days weaker | 10 trades, +0.15R vs +0.31R | Not on 1-hour data |

---

## 2–12. Every idea tested, and the decision

"WF" = 1-hour walk-forward, 9 folds; "x/9" = folds where the variant beat V2 on unseen data.

| # | Idea | 5-min result | Robustness check | Decision |
|---|---|---|---|---|
| 2 | Quality score ≥ 50…75 | Worse than V2 at **every** threshold (best: ≥60, ₹27k vs ₹34k) | 1-hour: ≥65/70 helped (WF 4/9, 5/9); buckets not ordered | **Rejected** as a gate; kept as a diagnostic |
| 3 | Trend agreement (short + higher TF) | −₹11,203 | WF 3/9 | **Rejected** |
| 3 | Sideways: reject / need score 75 | −₹15,650 / −₹21,769 | WF 3/9, 2/9 | **Rejected** |
| 3 | Drop V2's 15-min trend filter | slightly worse | WF 0/9 | **Keep V2's filter** |
| 4 | Regime filter (learned ALLOW/RESTRICT/BLOCK) | n < 6 in most regimes | WF 2/9, out-of-sample net ≈ 0 | **Rejected** |
| 5 | Stops: ATR ×0.75–1.5, recent swing, structure, 0.2/0.3 %, hybrid | **None beat V2's swing stop** (best: ATR×1.5, about equal) | ATR×1.0 / 0.3 % helped on hourly bars (WF 4–5/9) | **Keep V2 stop** |
| 6 | Targets 1:1.5 … 1:3 | 1:2.25 slightly better (₹41k) | WF 1/9 | **Keep 1:2** |
| 6 | 50 % at 1R, rest at 2.5R | ₹12,522 (worse) | WF 4/9, lower net | **Rejected** |
| 6 | Cost at 1R + trail from 1.5R | no gain | identical | **Rejected**; keep cost at 1R |
| 7 | **No entries before 10:00** | ₹54,634, PF 2.18, DD −₹12,685, worst day −₹8,269; **5/5** 11-day windows positive (V2: 2/5) | Training windows chose an early cut-off in **every** fold (10:00–11:00), but out-of-sample profit was lower than V2 (§13) | **Candidate** — the only entry change with support |
| 8 | Sensex filters (score ≥ 70/75) | worse (₹1k / ₹10k) | helped on hourly bars | **Rejected** |
| 9 | Market-quality filter (all thresholds) | worse at every setting (−₹13k to +₹3k) | worse everywhere; its "ALLOW" bucket is the worst in 4 of 5 tests | **Rejected** as a gate. Keep V2's binary crude veto, which helped (off: ₹28k, DD −₹20k) |
| 10 | Score-based sizing (≥75 → 4 lots, else 2) | ₹4,019 | WF 0/9 | **Rejected** |
| 10 | **Per-trade risk budget ₹6k** (loss at stop incl. costs ≤ ₹6k and ≤ what's left of the daily limit) | ₹39,807, DD −₹15,461 | Hourly: DD −₹83k → −₹29k, worst day −₹20k → −₹6k; same R expectancy | **Adopt as a risk rule** — it controls loss size, not edge |
| 10 | Daily loss ₹8k (V2 style, checked after trades) | **no effect** | no effect | V2's daily limit can't stop the 2nd trade's loss; only the pre-entry budget does |
| 11 | Loss streak: none / stop after 3 / half after 2 | removing the lock: +₹9k (2 extra trades) | no effect on hourly bars | **Keep V2** (stop after 2) — no evidence either way |
| 12 | News: block HIGH days (RBI/Fed calendar) | ₹52,420, PF 1.92 | 4 event days, 6 trades — too few to validate | **Adopt as a safety rule** (a priori, not fitted) |
| 12 | News: MEDIUM → half size | lower net, lower DD | tariff headlines make 27/54 days MEDIUM | **Rejected** (too noisy) |

---

## Best V3 configuration (`v3/v3_config.py`)

Everything else is identical to V2 (signal rules, 15-min trend filter, binary crude veto, swing stop, cost at 1R, 1:2 target, max 1 position, Nifty preferred).

| Parameter | V2 | V3 | Why |
|---|---|---|---|
| `ENABLE_TIME_FILTER` / `TIME_BLOCKS` | off | no entries before **10:00** | The only entry pattern that held in both halves and every training window |
| `ENABLE_RISK_BUDGET` | off | on | Caps the loss of each trade, enforced before entry |
| `MAX_OPEN_RISK` | — | **₹6,000** | 2 % of an assumed ₹3 lakh account, set in advance (₹5k looked better but was not chosen) |
| `MAX_DAILY_LOSS` | ₹12,000 (checked after trades) | ₹12,000 (**also before entry**) | Makes the daily limit actually limit the day |
| `LOTS` | 4 | up to 4 | The budget reduces lots when the stop is wide |

Kept switchable but **off**: `ENABLE_SCORE_FILTER`, `ENABLE_REGIME_FILTER`, `ENABLE_DYNAMIC_SL`, `ENABLE_DYNAMIC_TARGET`, `ENABLE_DYNAMIC_SIZING`, `ENABLE_MARKET_QUALITY`, `ENABLE_SENSEX_FILTER`, `ENABLE_NEWS_FILTER` (recommended on as a safety rule; see below).

---

## V2 vs V3 — full comparison (5-min, 55 days, in-sample)

V3's rules were chosen using this period, so treat these numbers as an upper bound.

| Metric | V2 | V2 + time filter | **V3** | V3 + dynamic SL | V3 + score sizing | V3 + news filter |
|---|---|---|---|---|---|---|
| Trades | 36 | 26 | **26** | 26 | 26 | 20 |
| Win rate | 38.9 % | 46.2 % | **46.2 %** | 46.2 % | 42.3 % | 45.0 % |
| Net P&L | ₹34,231 | ₹54,634 | **₹58,431** | ₹59,199 | ₹18,733 | ₹36,414 |
| Avg trade | ₹951 | ₹2,101 | **₹2,247** | ₹2,277 | ₹720 | ₹1,821 |
| Avg win / loss | ₹8,455 / −₹3,825 | ₹8,423 / −₹3,317 | **₹8,423 / −₹3,046** | ₹8,758 / −₹3,278 | ₹4,237 / −₹1,858 | ₹5,922 / −₹1,535 |
| Profit factor | 1.41 | 2.18 | **2.37** | 2.29 | 1.67 | 3.16 |
| Expectancy | +0.27R | +0.53R | **+0.53R** | +0.49R | +0.45R | +0.54R |
| Reward/risk | 2.21 | 2.54 | **2.77** | 2.67 | 2.28 | 3.86 |
| Max drawdown | −₹17,517 | −₹12,685 | **−₹10,629** | −₹11,331 | −₹8,849 | −₹6,542 |
| Recovery factor | 1.95 | 4.31 | **5.50** | 5.22 | 2.12 | 5.57 |
| Sharpe / Sortino | 2.03 / 3.30 | 3.78 / 8.87 | **4.10 / 10.40** | 3.73 / 8.87 | 2.83 / 4.93 | 4.00 / 10.35 |
| Worst day | −₹11,619 | −₹8,269 | **−₹6,291** | −₹8,173 | −₹6,213 | −₹6,213 |
| Stop-loss exits | 42 % | 31 % | **31 %** | 31 % | 31 % | 25 % |

Sharpe, Sortino and Calmar are annualised from 55 days and are **not** meaningful as long-run estimates. Calmar, streaks and winning/losing days are in `results.md`.

## Out-of-sample results (the ones that count)

**Walk-forward, 5-min.** Each training window (22 days) picks the entry cut-off and risk budget from 20 pre-declared options using an edge-and-recovery score in R (never P&L). The choice is then run untouched on the next 11 days.

| Test window | Chosen on training data | V3 net | V2 net | V3 DD | V2 DD |
|---|---|---|---|---|---|
| 24 Aug – 7 Sep | no entries before 11:00 | ₹20,493 | ₹28,134 | −₹3,925 | −₹4,192 |
| 8 – 23 Sep | before 10:30 | −₹4,241 | −₹2,455 | −₹10,334 | −₹14,945 |
| 24 Sep – 8 Oct | before 10:00 | ₹2,177 | ₹12,870 | −₹8,665 | −₹8,665 |
| **Pooled (33 days)** | | **₹18,429** · 15 trades · 40 % · PF 1.61 · 0.34R · worst day −₹8,269 | **₹38,549** · 23 trades · 43 % · PF 1.81 · 0.43R · worst day −₹10,730 | **−₹12,906** | **−₹17,517** |

The anchored version (training from day 1) gave the same result.

**Static train → validate → test:** training (23 Jul – 28 Aug) chose "no entries before 11:00". Validation (31 Aug – 17 Sep) **rejected** it (score −3.06 vs V2 1.55), so the test period runs V2.

**1-hour walk-forward (risk budget only):** 46 trades, same expectancy (0.05R vs 0.06R), drawdown −₹23,801 vs −₹83,376, worst day −₹5,394 vs −₹20,271, net ₹10,601 vs ₹55,393. The budget trades smaller, so losses shrink and so do profits.

## Monte Carlo (10,000 runs)

| | V2 (5-min WF OOS) | V3 (5-min WF OOS) | V2 (5-min full) | V3 (5-min full, in-sample) |
|---|---|---|---|---|
| Expected max drawdown | −₹18,127 | −₹15,278 | −₹25,899 | −₹14,800 |
| 95th-percentile drawdown | −₹29,877 | −₹24,787 | −₹42,406 | −₹23,995 |
| Worst simulated drawdown | −₹47,338 | −₹30,201 | −₹73,458 | −₹38,497 |
| P(negative 21-day month) | 14.3 % | 24.6 % | 27.4 % | 10.3 % |
| P(losing over the period) | 9.0 % | 19.1 % | 17.3 % | 2.1 % |
| 5th-percentile month | −₹12,987 | −₹16,102 | −₹24,053 | −₹5,898 |

Out-of-sample, V3 has smaller drawdowns but a **higher chance of a losing month**, because it trades less. The in-sample column looks far better, which is exactly the gap that marks overfitting.

## Cost scenarios (5-min, full period)

| Scenario | V2 net / PF | V3 net / PF |
|---|---|---|
| V2's own model (charges only) | ₹44,805 / 1.57 | ₹65,741 / 2.66 |
| Optimistic (0.1 % + 0.1 %) | ₹39,728 / 1.49 | ₹61,951 / 2.49 |
| Base (0.25 % + 0.25 %) | ₹34,231 / 1.41 | ₹58,431 / 2.37 |
| Conservative (0.5 % + 0.4 %, 1-bar delay) | ₹8,783 / 1.10 | ₹46,175 / 2.04 |
| Stress (1 % + 0.75 %, 1-bar delay) | **−₹16,108 / 0.83** | ₹34,898 / 1.77 |

V2 is very sensitive to execution quality. Fewer, later entries (V3) hold up better, but this is in-sample.

---

## NIFTY vs SENSEX — recommendation: keep both, Nifty preferred, no Sensex filter

- The −₹3,463 for Sensex in the V2 report is mostly an **interaction effect**. Traded alone over the same 55 days, Sensex made +₹29,875 (+0.16R) and Nifty alone +₹15,201 (+0.16R). In the combined run, Sensex takes the setups left after Nifty has used the single position slot and the daily limits.
- **Diversification is weak:** daily P&L correlation is 0.67 on 5-min and 0.89 on hourly bars, on days both traded.
- On hourly bars Sensex is clearly weaker (+0.07R vs +0.19R; negative in walk-forward), and score filters for Sensex helped there but hurt on 5-min.
- **Conclusion:** the evidence doesn't support removing Sensex, and doesn't support a Sensex filter. It is the first thing to revisit when more 5-min data exists. If you want simplicity, Nifty-only is defensible on the hourly evidence but cost 56 % of profit and raised drawdown on 5-min.

---

## Remaining weaknesses

1. **Sample size.** 36 trades (5-min) and 62 (hourly). No result here, good or bad, is statistically reliable.
2. **Option prices are modelled.** No real bid/ask, IV smile or intraday IV moves. Execution quality decides whether V2 makes or loses money (see stress costs).
3. **The 10:00 cut-off was found by looking at this data.** It held in both halves and in every training window, but the out-of-sample test didn't confirm an improvement in profit.
4. **Hourly data is a different trade profile** (74 % of trades exit at square-off). It can't test time-of-day, stops or targets for the 5-min strategy.
5. **The quality score, market-quality score and regime filter have no predictive value as designed.** Refitting their weights to 36 trades would be overfitting, so it wasn't done.
6. **News archive dates have no time of day**, so headline-based risk uses previous-day news only. Event-calendar dates for this period were confirmed from the archive; live use needs the official RBI and FOMC calendars entered in advance.
7. Index volume comes from the NIFTYBEES ETF (a proxy). Expiry weekdays and lot sizes are current values, applied to older hourly data.

## What to do now

1. **Don't switch the live bot to V3.** It didn't pass.
2. **Two risk controls are worth adopting now**, as safety rules rather than performance claims:
   - Check the daily loss limit **before** each entry, using the trade's loss at the stop. Today a second trade can push the day well past where you'd want it (worst V2 day: −₹11,619).
   - Block new entries on scheduled RBI-policy and Fed-decision days, using an event calendar you fill in from the official schedules.

   I can add both to the live bot if you want, with your go-ahead.
3. **Shadow-test V3 in paper trading** alongside V2 for 6–8 weeks. That's the cheapest way to get real out-of-sample data.
4. **Get more 5-minute history.** Kite Connect's historical API provides years of 5-minute index data. Re-running this research on 2–3 years (200–400 trades) is the single most valuable next step. The whole pipeline (`python -m v3.run_research`) is ready for it.
5. With real option fills from paper or live trading, replace the modelled premiums and re-check costs.

## Files

- `v3/` — research engine (features, regimes, score, market quality, news risk, costs, engine, metrics, Monte Carlo, `run_research.py`, `v3_config.py`, `events.csv`)
- `reports/v3/results.md` — every table behind this report
- `reports/v3/v2_trades_diagnostics.csv` — every V2 trade with indicators, regime, score, MFE/MAE, costs, R
- `reports/v3/v3_executed_trades.csv` / `v3_rejected_signals.csv` — trade-level diagnostics, with the reason each rejected signal was blocked
- `reports/v3/v2_trades_original.csv` — your original `backtest_trades.csv`, preserved
