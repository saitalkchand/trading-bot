# Scalper V2 vs current scalper

NIFTY · 18 trading days (2026-09-29 → 2026-10-26) · 1 lot · same 1-min data, option model, fills and engine for every test.

## 1. Headline — every test under all three fill models

|                                           |   trades |   win_rate |    net |   pf |   avg_trade |   expectancy_R |   max_dd |   worst_day |   charges |
|:------------------------------------------|---------:|-----------:|-------:|-----:|------------:|---------------:|---------:|------------:|----------:|
| A current scalper · charges only          |       76 |       44.7 |  -2475 | 0.83 |         -32 |         -0.1   |    -5090 |       -1155 |      5477 |
| A current scalper · base                  |       75 |       41.3 |  -9907 | 0.44 |        -132 |         -0.406 |    -9907 |       -1381 |      5397 |
| A current scalper · conservative          |       60 |       13.3 | -14134 | 0.21 |        -235 |         -0.725 |   -14134 |       -1299 |      4291 |
| B V2 strict (spec-literal) · charges only |        2 |       50   |    156 | 1.38 |          78 |          0.239 |     -408 |         156 |       169 |
| B V2 strict (spec-literal) · base         |        1 |        0   |   -484 | 0    |        -484 |         -1.49  |     -484 |        -484 |        83 |
| B V2 strict (spec-literal) · conservative |        1 |        0   |   -546 | 0    |        -546 |         -1.68  |     -546 |        -546 |        83 |
| C V2 no agents · charges only             |        6 |       50   |    426 | 1.34 |          71 |          0.25  |     -455 |        -455 |       497 |
| C V2 no agents · base                     |        5 |       40   |   -945 | 0.36 |        -189 |         -0.537 |    -1005 |        -521 |       410 |
| C V2 no agents · conservative             |        4 |        0   |  -1672 | 0    |        -418 |         -1.225 |    -1672 |        -574 |       327 |
| D V2 + agents (crude) · charges only      |        6 |       50   |    426 | 1.34 |          71 |          0.25  |     -455 |        -455 |       497 |
| D V2 + agents (crude) · base              |        5 |       40   |   -945 | 0.36 |        -189 |         -0.537 |    -1005 |        -521 |       410 |
| D V2 + agents (crude) · conservative      |        4 |        0   |  -1672 | 0    |        -418 |         -1.225 |    -1672 |        -574 |       327 |

## 2. Full metrics, base fills

|                            |   trades |   wins |   losses |   win_rate |   gross |   charges |   net |   pf |   avg_trade |   expectancy_R |   max_dd |   worst_day |   avg_hold |   avg_win |   avg_loss |   largest_win |   largest_loss |   max_consec_losses |
|:---------------------------|---------:|-------:|---------:|-----------:|--------:|----------:|------:|-----:|------------:|---------------:|---------:|------------:|-----------:|----------:|-----------:|--------------:|---------------:|--------------------:|
| A current scalper          |       75 |     31 |       44 |       41.3 |   -4512 |      5397 | -9907 | 0.44 |        -132 |         -0.406 |    -9907 |       -1381 |        2.4 |       252 |       -403 |           560 |           -479 |                   5 |
| B V2 strict (spec-literal) |        1 |      0 |        1 |        0   |    -401 |        83 |  -484 | 0    |        -484 |         -1.49  |     -484 |        -484 |        1   |         0 |       -484 |          -484 |           -484 |                   1 |
| C V2 no agents             |        5 |      2 |        3 |       40   |    -534 |       410 |  -945 | 0.36 |        -189 |         -0.537 |    -1005 |        -521 |        3.8 |       268 |       -494 |           491 |           -521 |                   2 |
| D V2 + agents (crude)      |        5 |      2 |        3 |       40   |    -534 |       410 |  -945 | 0.36 |        -189 |         -0.537 |    -1005 |        -521 |        3.8 |       268 |       -494 |           491 |           -521 |                   2 |

## 3. Diagnostics — current scalper (A), base fills

**index × side**

|          |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------|---------:|-----------:|-----:|------:|------------:|---------:|
| NIFTY CE |       36 |       44.4 | 0.62 | -2952 |         -82 |    -2952 |
| NIFTY PE |       39 |       38.5 | 0.3  | -6955 |        -178 |    -6955 |

**CE vs PE**

|    |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---|---------:|-----------:|-----:|------:|------------:|---------:|
| CE |       36 |       44.4 | 0.62 | -2952 |         -82 |    -2952 |
| PE |       39 |       38.5 | 0.3  | -6955 |        -178 |    -6955 |

**NIFTY vs SENSEX**

|       |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:------|---------:|-----------:|-----:|------:|------------:|---------:|
| NIFTY |       75 |       41.3 | 0.44 | -9907 |        -132 |    -9907 |

**time of day**

|                      |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------------------|---------:|-----------:|-----:|------:|------------:|---------:|
| afternoon (>13:30)   |        9 |       33.3 | 0.23 | -2032 |        -225 |    -2156 |
| midday (11:30–13:30) |       28 |       28.6 | 0.22 | -5804 |        -207 |    -6030 |
| morning (<11:30)     |       38 |       52.6 | 0.73 | -2071 |         -54 |    -4138 |

**volatility (1-min ATR% vs median)**

|          |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------|---------:|-----------:|-----:|------:|------------:|---------:|
| high vol |       50 |         38 | 0.4  | -7508 |        -150 |    -7508 |
| low vol  |       25 |         48 | 0.54 | -2399 |         -95 |    -2895 |

**entry type**

|           |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:----------|---------:|-----------:|-----:|------:|------------:|---------:|
| ema-cross |       75 |       41.3 | 0.44 | -9907 |        -132 |    -9907 |

**exit reason**

|              |   trades |   win_rate |     pf |    net |   avg_trade |   max_dd |
|:-------------|---------:|-----------:|-------:|-------:|------------:|---------:|
| stop at cost |       22 |       77.3 |   0.68 |   -280 |         -12 |     -445 |
| stop-loss    |       38 |        0   |   0    | -16798 |        -442 |   -16798 |
| target       |       14 |      100   | inf    |   7236 |         516 |        0 |
| time stop    |        1 |        0   |   0    |    -65 |         -65 |      -65 |


## 3. Diagnostics — V2 (C), base fills

**index × side**

|          |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------|---------:|-----------:|-----:|------:|------------:|---------:|
| NIFTY CE |        1 |          0 | 0    |  -477 |        -477 |     -477 |
| NIFTY PE |        4 |         50 | 0.53 |  -468 |        -117 |    -1005 |

**CE vs PE**

|    |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---|---------:|-----------:|-----:|------:|------------:|---------:|
| CE |        1 |          0 | 0    |  -477 |        -477 |     -477 |
| PE |        4 |         50 | 0.53 |  -468 |        -117 |    -1005 |

**NIFTY vs SENSEX**

|       |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:------|---------:|-----------:|-----:|------:|------------:|---------:|
| NIFTY |        5 |         40 | 0.36 |  -945 |        -189 |    -1005 |

**time of day**

|                      |   trades |   win_rate |    pf |   net |   avg_trade |   max_dd |
|:---------------------|---------:|-----------:|------:|------:|------------:|---------:|
| afternoon (>13:30)   |        1 |        100 | inf   |   491 |         491 |        0 |
| midday (11:30–13:30) |        2 |         50 |   0.1 |  -431 |        -215 |     -477 |
| morning (<11:30)     |        2 |          0 |   0   | -1005 |        -502 |    -1005 |

**volatility (1-min ATR% vs median)**

|          |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------|---------:|-----------:|-----:|------:|------------:|---------:|
| high vol |        1 |          0 | 0    |  -521 |        -521 |     -521 |
| low vol  |        4 |         50 | 0.56 |  -424 |        -106 |     -484 |

**entry type**

|          |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:---------|---------:|-----------:|-----:|------:|------------:|---------:|
| pullback |        5 |         40 | 0.36 |  -945 |        -189 |    -1005 |

**regime at entry**

|                  |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:-----------------|---------:|-----------:|-----:|------:|------------:|---------:|
| STRONG_DOWNTREND |        3 |       66.7 | 1.03 |    16 |           5 |     -521 |
| STRONG_UPTREND   |        1 |        0   | 0    |  -477 |        -477 |     -477 |
| WEAK_DOWNTREND   |        1 |        0   | 0    |  -484 |        -484 |     -484 |

**exit reason**

|              |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:-------------|---------:|-----------:|-----:|------:|------------:|---------:|
| stop at cost |        1 |        100 |  inf |    46 |          46 |        0 |
| stop-loss    |        3 |          0 |    0 | -1482 |        -494 |    -1482 |
| target       |        1 |        100 |  inf |   491 |         491 |        0 |


## 4. Where the current scalper loses (regime from V2's detector at entry)

|                  |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:-----------------|---------:|-----------:|-----:|------:|------------:|---------:|
| HIGH_VOLATILITY  |        5 |       40   | 0.45 |  -702 |        -140 |     -854 |
| RANGE            |       28 |       46.4 | 0.75 | -1475 |         -52 |    -2192 |
| STRONG_DOWNTREND |        1 |        0   | 0    |  -460 |        -460 |     -460 |
| WEAK_DOWNTREND   |       21 |       38.1 | 0.14 | -4629 |        -220 |    -4629 |
| WEAK_UPTREND     |       20 |       40   | 0.45 | -2641 |        -132 |    -2641 |

|                         |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:------------------------|---------:|-----------:|-----:|------:|------------:|---------:|
| NIFTY CE range/high-vol |       16 |       50   | 0.9  |  -311 |         -19 |     -990 |
| NIFTY CE trend          |       20 |       40   | 0.45 | -2641 |        -132 |    -2641 |
| NIFTY PE range/high-vol |       17 |       41.2 | 0.54 | -1866 |        -109 |    -2566 |
| NIFTY PE trend          |       22 |       36.4 | 0.13 | -5089 |        -231 |    -5089 |

|                         |   trades |   win_rate |   pf |   net |   avg_trade |   max_dd |
|:------------------------|---------:|-----------:|-----:|------:|------------:|---------:|
| CE afternoon (>13:30)   |        3 |       33.3 | 0.64 |  -299 |         -99 |     -841 |
| CE midday (11:30–13:30) |       14 |       28.6 | 0.32 | -2271 |        -162 |    -3055 |
| CE morning (<11:30)     |       19 |       57.9 | 0.9  |  -382 |         -20 |    -1826 |
| PE afternoon (>13:30)   |        6 |       33.3 | 0.04 | -1733 |        -288 |    -1733 |
| PE midday (11:30–13:30) |       14 |       28.6 | 0.15 | -3533 |        -252 |    -3533 |
| PE morning (<11:30)     |       19 |       47.4 | 0.57 | -1689 |         -88 |    -2527 |


## 5. Why V2 setups were rejected (base fills, setups with an entry trigger)

| reason                                      |   count |
|:--------------------------------------------|--------:|
| too far from VWAP                           |     344 |
| weak downtrend needs score >= 85            |     226 |
| weak uptrend needs score >= 85              |     160 |
| regime not downtrend                        |     132 |
| regime not uptrend                          |     126 |
| opening range too wide                      |     104 |
| wrong side of VWAP                          |      65 |
| weak/wicky/extended candle                  |      34 |
| VIX regime                                  |      11 |
| score below minimum                         |       4 |
| outside entry window                        |       3 |
| position already open                       |       2 |
| loss at stop exceeds today's remaining risk |       2 |


_Runtime 16s. Trades: reports/scalper_v2/trades_*.csv, rejections: rejections_v2.csv._