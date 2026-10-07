# AI red-team audit: backtest harness

Generated 2026-10-07T16:13:52.582001+00:00 | mode: dry-run | contract v1.0

Scope: `backtest/backtester.py`, `backtest/fill_models.py`, `backtest/ev_gate.py`, `backtest/threshold_sensitivity.py` -- code and documented assumptions. The audit targets lookahead bias, survivorship bias, and unrealistic fill assumptions.

## Summary

| severity | count |
|---|---|
| HIGH | 2 |
| MEDIUM | 3 |
| LOW | 1 |
| INFO | 6 |

## Findings

### [HIGH] Entry price is a normal-CDF moneyness proxy, not a measured Kalshi ask

- id: `entry-price-modeled` | category: fills | status: flag | source: checklist

Historical Kalshi order books are unavailable, so the backtest prices entries with P(win) under N(spot, sigma) rounded to cents. If the real book charges more (wider spreads, worse queue), every PnL number is optimistic. The assumption is documented in the module docstring, but nothing in the harness bounds the error -- the lake's captured books are the planned fix.

Evidence:
```
backtest/backtester.py:29: moneyness, rounded to whole cents. This is a *stand-in for the Kalshi
backtest/backtester.py:153: def implied_win_prob(spot: float, strike: float, sigma_w: float) -> float:
```

### [HIGH] Main backtest path assumes full fills at the proxied price

- id: `full-fill-assumption` | category: fills | status: flag | source: checklist

run_backtest fills every entered window in full at price_cents: no queue position, no partial fills, no adverse selection, no slippage beyond fees. fill_models.py builds the conservative counterpart, but the headline PnL, hit-rate and drawdown numbers come from the optimistic path. Paper PnL should be reported under both assumptions side by side.

Evidence:
```
backtest/backtester.py:314: count = max_affordable_contracts(budget_cents, price_cents, "taker")
```

### [MEDIUM] Neutral-regime side defaults to 'yes' with no measured edge

- id: `neutral-fallback-arbitrary` | category: assumptions | status: flag | source: checklist

When RSI is neutral the bot still trades, defaulting to the side nearest the strike. There is no evidence this default has edge; the EV gate (ev_gate.py) exists to address exactly this but is not wired into the backtest entry path.

Evidence:
```
backtest/backtester.py:183: neutral_fallback_side: str = "yes",
backtest/backtester.py:193: self.neutral_fallback_side = neutral_fallback_side
```

### [MEDIUM] The live spread gate has no backtest equivalent (spreads unobservable)

- id: `no-spread-gate` | category: assumptions | status: flag | source: checklist

The live bot can refuse wide spreads; the backtest cannot replay them, so it trades windows the live bot might skip. Direction of bias: unknown, depends on whether wide-spread windows are systematically worse.

Evidence:
```
backtest/backtester.py:61: the budget cannot afford one contract. There is no spread gate in the
```

### [MEDIUM] Strikes come from a synthetic round-number grid, not historical Kalshi strikes

- id: `synthetic-strike-grid` | category: assumptions | status: flag | source: checklist

History has no Kalshi markets, so strikes are snapped to a --strike-step-usd grid. Real Kalshi BTC-15M strikes sit on round levels, so the grid is a reasonable stand-in, but a different grid changes which windows trade and at what moneyness.

Evidence:
```
backtest/backtester.py:163: def synthetic_strike(spot: float, regime: str, step_usd: float) -> float:
```

### [LOW] Live bot may read the in-progress candle; the backtest does not

- id: `live-partial-candle` | category: lookahead | status: note | source: checklist

Stated difference in the harness docstring (Assumption 5). The backtest is the cleaner of the two; any live-vs-backtest mismatch here flatters the backtest slightly and should be closed by dropping the partial candle live.

Evidence:
```
backtest/backtester.py:56: and may include the in-progress candle; this is a stated difference in
```

### [INFO] Decision snapshot uses only candles strictly before window open

- id: `decision-no-lookahead` | category: lookahead | status: pass | source: checklist

The decision index is bisect_left(ts, w_start): candles starting at or after the window open are excluded, and the lookback slice closes[i_start:i_end] ends at the last closed candle. No future information enters the signal.

Evidence:
```
backtest/backtester.py:280: i_end = bisect.bisect_left(ts, w_start)
```

### [INFO] Backtest core is deterministic (no RNG in the decision path)

- id: `determinism` | category: hygiene | status: pass | source: checklist

run_backtest uses no randomness: same candles + params => same report. (Randomness appears only in fill_models' conservative simulator, which is explicitly stochastic and seeded by the caller.)

Evidence:
```
backtest/backtester.py: no RNG imports
```

### [INFO] Taker fees are deducted on every fill via the bot's own fee function

- id: `fee-model-applied` | category: fills | status: pass | source: checklist

Every traded window subtracts fee_cents_taker(count, price_cents), the same ceiling-rounding taker model the live bot uses.

Evidence:
```
backtest/backtester.py:329: fee_cents = fee_cents_taker(count, price_cents)
```

### [INFO] RSI is evaluated on the last closed candle, not the in-progress one

- id: `rsi-closed-candles` | category: lookahead | status: pass | source: checklist

rsi_vals is indexed at i_end - 1 (the newest candle strictly before the window open). The in-progress candle never enters the backtest signal.

Evidence:
```
backtest/backtester.py:285: rsi = rsi_vals[i_end - 1]
```

### [INFO] Settlement reads the window-close candle, which is the contract resolution

- id: `settlement-is-resolution` | category: lookahead | status: note | source: checklist

Settlement uses candles inside [w_start, w_end) -- data that only exists after the decision. For a held-to-expiry binary this is the resolution mechanism, not lookahead bias; it would only be bias if the decision used it.

Evidence:
```
backtest/backtester.py:325: settle_close = closes[s_end - 1]
```

### [INFO] No historical Kalshi data is consumed, so survivorship bias cannot enter

- id: `survivorship-na` | category: survivorship | status: flag | source: checklist

The backtest replays Coinbase candles only; delisted/expired Kalshi markets never enter the sample because no Kalshi history is used. The day the lake starts feeding real historical books, this check must be re-run: expired markets must be retained in the sample.

Evidence:
```
backtest/backtester.py: no reference to historical Kalshi data (regex search: found)
```

## Methodology note

Checklist mode encodes twelve targeted checks; each check's reasoning is fixed, but the evidence quotes are extracted from the audited source at run time, so the report goes stale if the code changes -- re-run the audit after any backtest change. LLM mode sends the same sources to an external reviewer and merges its findings; treat LLM findings as leads, not verdicts.
