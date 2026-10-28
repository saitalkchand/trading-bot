# Scalper strategy lab — results

NIFTY · 2026-09-17 → 2026-10-14 (19 trading days) · 1 lot · dataset `snapshot_2026-09-16_2026-10-14.pkl` (sha256 f45b46931d187656) · generated 2026-10-26 17:35

Paper research only. Option prices are modelled (Black-Scholes, IV = previous day's India VIX); volume is NIFTYBEES.NS; there are no historical option quotes, so spread is a modelled assumption.

## 1. Baseline reproduction

| fills | old engine trades | old net | lab engine trades | lab net | identical trades |
|---|---|---|---|---|---|
| base | 75 | -8,256 | 75 | -8,256 | True |
| conservative | 38 | -8,198 | 38 | -8,198 | True |

## 2. All experiments (lab engine)

| experiment | fills | trades | win % | net ₹ | PF | exp ₹/trade (95% CI) | exp R | avg win | avg loss | max DD | P(total>0) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A baseline (volume rule, 5/10) | base | 69 | 47.8 | -8,927 | 0.42 | -129 (-207 to -41) | -0.398 | 191 | -423 | -9,462 | 0.0 |
| A baseline (volume rule, 5/10) | conservative | 38 | 15.8 | -8,417 | 0.26 | -221 (-332 to -102) | -0.682 | 489 | -354 | -8,417 | 0.0 |
| B sweep reversal | base | 7 | 42.9 | -454 | 0.7 | -64 (-334 to 235) | -0.200 | 347 | -374 | -615 | 0.34 |
| B sweep reversal | conservative | 7 | 28.6 | -1,124 | 0.35 | -160 (-385 to 98) | -0.494 | 301 | -345 | -1,124 | 0.11 |
| B2 sweep reversal + structure stop | base | 7 | 42.9 | -1,014 | 0.62 | -144 (-569 to 401) | -0.327 | 540 | -658 | -1,720 | 0.28 |
| B2 sweep reversal + structure stop | conservative | 7 | 28.6 | -1,438 | 0.49 | -205 (-613 to 329) | -0.438 | 685 | -561 | -1,950 | 0.2 |
| C breakout-retest | base | 5 | 20.0 | -586 | 0.47 | -117 (-384 to 198) | -0.361 | 514 | -275 | -1,100 | 0.25 |
| C breakout-retest | conservative | 5 | 20.0 | -1,196 | 0.28 | -239 (-504 to 148) | -0.736 | 468 | -416 | -1,665 | 0.09 |
| C2 breakout, no retest | base | 26 | 34.6 | -2,943 | 0.52 | -113 (-259 to 34) | -0.348 | 360 | -363 | -2,943 | 0.07 |
| C2 breakout, no retest | conservative | 23 | 21.7 | -4,745 | 0.34 | -206 (-356 to -37) | -0.635 | 491 | -400 | -4,745 | 0.01 |
| C3 breakout-retest + structure stop | base | 3 | 66.7 | 413 | 1.51 | 137 (-805 to 1198) | +0.212 | 609 | -805 | -805 | 0.64 |
| C3 breakout-retest + structure stop | conservative | 3 | 33.3 | 292 | 1.34 | 97 (-849 to 1149) | +0.153 | 1149 | -428 | -857 | 0.6 |
| D baseline + regime filter | base | 70 | 51.4 | -6,250 | 0.56 | -89 (-175 to 0) | -0.275 | 219 | -415 | -6,752 | 0.03 |
| D baseline + regime filter | conservative | 38 | 15.8 | -8,042 | 0.27 | -211 (-320 to -91) | -0.651 | 489 | -343 | -8,042 | 0.0 |
| E0 baseline, no volume requirement | base | 67 | 44.8 | -9,983 | 0.35 | -149 (-227 to -67) | -0.458 | 177 | -413 | -11,186 | 0.0 |
| E0 baseline, no volume requirement | conservative | 44 | 18.2 | -8,099 | 0.32 | -184 (-287 to -69) | -0.566 | 478 | -331 | -8,099 | 0.0 |
| E1 volume 2.0x | base | 61 | 44.3 | -8,221 | 0.41 | -134 (-217 to -45) | -0.415 | 211 | -409 | -9,598 | 0.0 |
| E1 volume 2.0x | conservative | 40 | 17.5 | -8,044 | 0.3 | -201 (-315 to -83) | -0.619 | 496 | -349 | -8,044 | 0.0 |
| E2 volume 1.5x + displacement | base | 70 | 47.1 | -10,116 | 0.36 | -144 (-220 to -63) | -0.445 | 175 | -429 | -10,165 | 0.0 |
| E2 volume 1.5x + displacement | conservative | 41 | 12.2 | -9,285 | 0.21 | -226 (-320 to -112) | -0.697 | 495 | -326 | -9,285 | 0.0 |
| E3 volume 1.5x + retest confirm | base | 1 | 0.0 | -97 | 0.0 | -97 (-97 to -97) | -0.300 | 0 | -97 | -97 | 0.0 |
| E3 volume 1.5x + retest confirm | conservative | 1 | 0.0 | -154 | 0.0 | -154 (-154 to -154) | -0.476 | 0 | -154 | -154 | 0.0 |
| F1 baseline + volatility stop | base | 61 | 42.6 | -9,142 | 0.35 | -149 (-230 to -61) | -0.479 | 190 | -402 | -9,142 | 0.0 |
| F1 baseline + volatility stop | conservative | 42 | 16.7 | -8,028 | 0.35 | -191 (-307 to -48) | -0.662 | 625 | -354 | -8,979 | 0.01 |
| F2 baseline + structure stop | base | 44 | 40.9 | -8,400 | 0.41 | -190 (-337 to -29) | -0.425 | 322 | -546 | -11,849 | 0.01 |
| F2 baseline + structure stop | conservative | 37 | 18.9 | -8,959 | 0.34 | -242 (-404 to -62) | -0.531 | 670 | -455 | -11,068 | 0.0 |
| F3 baseline + ATR trail | base | 69 | 47.8 | -8,672 | 0.43 | -125 (-204 to -37) | -0.387 | 199 | -423 | -9,340 | 0.0 |
| F3 baseline + ATR trail | conservative | 38 | 15.8 | -8,417 | 0.26 | -221 (-332 to -102) | -0.682 | 489 | -354 | -8,417 | 0.0 |
| F4 baseline + profit lock | base | 69 | 47.8 | -8,762 | 0.43 | -126 (-205 to -38) | -0.391 | 196 | -423 | -9,298 | 0.0 |
| F4 baseline + profit lock | conservative | 41 | 22.0 | -8,631 | 0.29 | -210 (-318 to -94) | -0.648 | 396 | -381 | -8,631 | 0.0 |
| F5 baseline + target extension | base | 69 | 47.8 | -8,117 | 0.47 | -117 (-201 to -23) | -0.362 | 216 | -423 | -8,813 | 0.01 |
| F5 baseline + target extension | conservative | 41 | 19.5 | -8,775 | 0.28 | -214 (-322 to -95) | -0.659 | 428 | -369 | -8,775 | 0.0 |
| F6 baseline, no break-even | base | 65 | 38.5 | -6,344 | 0.64 | -97 (-205 to 6) | -0.300 | 447 | -438 | -8,500 | 0.03 |
| F6 baseline, no break-even | conservative | 45 | 28.9 | -8,884 | 0.41 | -197 (-320 to -65) | -0.607 | 474 | -470 | -9,138 | 0.0 |
| H1 volume rule · 15/30 | base | 61 | 39.3 | -18,694 | 0.48 | -306 (-546 to -56) | -0.314 | 712 | -967 | -21,675 | 0.01 |
| H1 volume rule · 15/30 | conservative | 52 | 26.9 | -18,285 | 0.44 | -351 (-609 to -70) | -0.361 | 1034 | -862 | -20,878 | 0.01 |
| H2 sweep reversal · 15/30 | base | 7 | 57.1 | 2,559 | 1.82 | 365 (-623 to 1245) | +0.375 | 1419 | -1038 | -2,013 | 0.78 |
| H2 sweep reversal · 15/30 | conservative | 7 | 57.1 | 2,046 | 1.63 | 292 (-676 to 1166) | +0.300 | 1329 | -1090 | -2,125 | 0.73 |
| H3 breakout-retest · 15/30 | base | 5 | 60.0 | -341 | 0.84 | -68 (-876 to 869) | -0.070 | 620 | -1101 | -2,202 | 0.38 |
| H3 breakout-retest · 15/30 | conservative | 5 | 20.0 | -558 | 0.76 | -111 (-920 to 821) | -0.115 | 1754 | -578 | -2,312 | 0.37 |
| H4 breakout, no retest · 15/30 | base | 21 | 57.1 | 4,450 | 1.51 | 211 (-287 to 711) | +0.217 | 1095 | -965 | -3,574 | 0.8 |
| H4 breakout, no retest · 15/30 | conservative | 21 | 42.9 | 3,440 | 1.38 | 163 (-328 to 666) | +0.168 | 1395 | -759 | -3,769 | 0.73 |
| H5 volume rule + regime filter · 15/30 | base | 61 | 44.3 | -14,724 | 0.55 | -241 (-474 to -4) | -0.248 | 660 | -957 | -18,501 | 0.02 |
| H5 volume rule + regime filter · 15/30 | conservative | 50 | 26.0 | -19,451 | 0.39 | -389 (-638 to -115) | -0.399 | 940 | -856 | -22,252 | 0.0 |
| H0 Scalper V2 (active rule) · 15/30 | base | 11 | 63.6 | 2,879 | 1.65 | 261 (-434 to 1017) | +0.269 | 1047 | -1114 | -3,326 | 0.74 |
| H0 Scalper V2 (active rule) · 15/30 | conservative | 10 | 40.0 | 3,423 | 1.96 | 342 (-360 to 1105) | +0.351 | 1747 | -594 | -2,345 | 0.81 |
| G selected combination | base | 24 | 29.2 | -3,399 | 0.52 | -141 (-307 to 42) | -0.436 | 532 | -419 | -3,596 | 0.06 |
| G selected combination | conservative | 21 | 23.8 | -4,728 | 0.34 | -225 (-381 to -38) | -0.693 | 491 | -449 | -4,728 | 0.01 |
| G* sweep + regime filter (exploratory) | base | 7 | 42.9 | -454 | 0.7 | -64 (-334 to 235) | -0.200 | 347 | -374 | -615 | 0.34 |
| G* sweep + regime filter (exploratory) | conservative | 7 | 28.6 | -1,124 | 0.35 | -160 (-385 to 98) | -0.494 | 301 | -345 | -1,124 | 0.11 |
| G* breakout-retest + regime filter (exploratory) | base | 5 | 20.0 | -586 | 0.47 | -117 (-384 to 198) | -0.361 | 514 | -275 | -1,100 | 0.25 |
| G* breakout-retest + regime filter (exploratory) | conservative | 5 | 20.0 | -1,196 | 0.28 | -239 (-504 to 148) | -0.736 | 468 | -416 | -1,665 | 0.09 |

## 3. Walk-forward (anchored; chosen on earlier days, scored on unseen later days)

Folds: train 2026-09-17→2026-09-25 / test 2026-09-28→2026-09-30; train 2026-09-17→2026-09-30 / test 2026-10-01→2026-10-06; train 2026-09-17→2026-10-06 / test 2026-10-07→2026-10-09; train 2026-09-17→2026-10-09 / test 2026-10-12→2026-10-14

| family | fills | OOS trades | OOS net ₹ | OOS exp R | PF | P(total>0) |
|---|---|---|---|---|---|---|
| baseline | base | 42 | -7,974 | -0.584 | 0.27 | 0.0 |
| baseline | conservative | 23 | -5,961 | -0.798 | 0.19 | 0.0 |
| sweep | base | 9 | -902 | -0.309 | 0.54 | 0.23 |
| sweep | conservative | 9 | -1,652 | -0.565 | 0.27 | 0.05 |
| retest | base | 5 | -586 | -0.361 | 0.47 | 0.25 |
| retest | conservative | 5 | -1,196 | -0.736 | 0.28 | 0.09 |
| baseline+regime | base | 44 | -6,490 | -0.454 | 0.38 | 0.0 |
| baseline+regime | conservative | 23 | -5,982 | -0.800 | 0.19 | 0.0 |
| volume variants | base | 38 | -7,930 | -0.642 | 0.26 | 0.0 |
| volume variants | conservative | 24 | -6,325 | -0.811 | 0.19 | 0.0 |

## 4. Components that beat the baseline on both fill models

C2 breakout, no retest, D baseline + regime filter, F4 baseline + profit lock, F5 baseline + target extension, F6 baseline, no break-even


## 5. Does NIFTYBEES volume carry information about NIFTY?

7368 completed 1-min candles. Correlation of relative volume with the size of the next 5-min move: 0.02; with continuation in the candle's direction: -0.012.

| rvol quintile | median rvol | candles | next-5-min |move| pts | continuation pts |
|---|---|---|---|---|
| Q1 lowest | 0.23 | 1474 | 9.99 | 0.43 |
| Q2 | 0.45 | 1473 | 10.41 | 0.59 |
| Q3 | 0.67 | 1474 | 10.48 | 0.74 |
| Q4 | 1.03 | 1473 | 10.7 | 0.33 |
| Q5 highest | 2.15 | 1474 | 11.5 | -0.02 |

NIFTY futures / option volume: not available historically on the free feed, and this Kite key gets PermissionException for quotes/historical data, so the comparison with futures volume could not be run.
