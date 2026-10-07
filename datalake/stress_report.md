# Adversarial stress-test report

Generated 2026-10-07T16:15:34.403858+00:00 | seeds per scenario: 5 | contract v1.0

Candle scenarios synthesize 7 days of 60s candles (seeded GBM) and run them through `backtest.backtester.run_backtest` with default params. Book scenarios run `optimistic_fill` vs `conservative_fill` on the scenario ladder. Drawdown is the max peak-to-trough of the traded equity curve, in dollars.

## Candle scenarios: PnL and drawdown distribution

| scenario | pnl mean $ | pnl p5 $ | pnl p95 $ | drawdown mean $ | drawdown p95 $ | drawdown max $ | hit rate | traded/wk |
|---|---|---|---|---|---|---|---|---|
| baseline | -213.3 | -768.69 | 110.87 | 426.26 | 838.86 | 920.49 | 0.4611 | 671.0 |
| flash_crash | -279.95 | -925.71 | 151.03 | 478.32 | 983.35 | 1078.16 | 0.4555 | 671.0 |
| trading_halt | -204.47 | -756.93 | 116.27 | 424.67 | 832.5 | 912.54 | 0.4616 | 669.0 |
| trend_day | -399.17 | -781.31 | -136.93 | 526.32 | 845.56 | 900.17 | 0.4486 | 671.0 |
| volatility_shock | -235.98 | -710.87 | 142.03 | 431.54 | 777.62 | 805.65 | 0.4596 | 671.0 |

## Book scenarios: fill stress

| scenario | model | fill rate (mean) | fill rate (min) | avg price c (mean) |
|---|---|---|---|---|
| normal_book | optimistic | 1.0 | 1.0 | 48.0 |
| normal_book | conservative | 1.0 | 1.0 | 48.0 |
| thin_book | optimistic | 0.4 | 0.4 | 53.25 |
| thin_book | conservative | 0.08 | 0.0 | 55.0 |
| wide_spread | optimistic | 0.0 | 0.0 | None |
| wide_spread | conservative | 0.0 | 0.0 | None |

## Reading this

- The backtest's headline numbers are conditional on its documented assumptions (modeled entry prices, full fills). These scenarios probe where those assumptions break: thin books cut the conservative fill rate, volatility shocks widen the drawdown distribution.
- `trading_halt` shows the harness skips gap windows without crashing; check `windows_skipped_mean` in the JSON for the exact count.
