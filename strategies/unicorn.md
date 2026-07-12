# Strategy: ICT Unicorn Model (Breaker/OB + FVG overlap) — ES/NQ

**ID:** `unicorn`
**Status:** draft
**Author:** kingkag3
**Created:** 2026-07-11

A displacement that **breaks structure** and leaves a **Fair Value Gap** which
**overlaps an order block / breaker** creates a high-conviction "Unicorn" zone;
we enter with a limit at the FVG **Consequent Encroachment (CE)**.

> **⚠️ Definitions are load-bearing.** Every ICT primitive here (FVG, swing,
> displacement, BOS/MSS, order block, breaker, overlap) is discretionary in the
> source material. This spec pins **one** mechanical definition for each. Results
> depend on those choices; SMC backtests are unusually prone to overfitting and
> hindsight bias — treat conclusions cautiously and prefer OOS + robustness.

---

## 1. Thesis
A strong displacement that breaks market structure marks a shift in order flow.
The imbalance it leaves (FVG) plus a reclaimed order block (breaker) at the same
price is a zone institutions may revisit to fill orders. Entering at the CE of
that overlap targets a continuation/reversal move toward the next liquidity, with
tight risk beyond the zone. Edge is **structural/behavioral** (imbalance fill +
stop placement around obvious blocks).

**Expected to be rare ("A+")** → few trades → statistical significance is a real
limitation, unlike ORB.

## 2. Universe
- CME index futures **ES / NQ** (reuse cached Dukascopy 5m). Tick/point values per
  `config.INSTRUMENTS`.
- Intraday, RTH, no overnight.

## 3. Timeframe & Session
- **Single timeframe v1:** `exec_tf = 5m` (detection + entry both on 5m).
- RTH `09:30–16:00 ET`; `entry_cutoff = 15:30`, `eod_flat = 15:55`.
- Optional `session_filter` (killzone) — off in v1 baseline.

## 4. Data
5m OHLCV (Dukascopy index CFD, near-24h; RTH slice used). Volume present but not
required by v1 logic.

## 5. Mechanical definitions (the core — fully quantified)

### 5.1 Swings (fractals)
`swing_high[s]` = `high[s]` is the strict max of `high[s-N .. s+N]` (`swing_n
N=2`); mirror for `swing_low`. **Confirmed with N-bar lag** → in the bar loop a
swing at `s` is only usable at bars `>= s+N` (no look-ahead).

### 5.2 Fair Value Gap (3-bar imbalance)
At bar `t` (using `t-2, t-1, t`):
- **Bullish FVG:** `low[t] > high[t-2]` → zone `[high[t-2], low[t]]`.
- **Bearish FVG:** `high[t] < low[t-2]` → zone `[high[t], low[t-2]]`.
- **CE** = midpoint of the zone. Require zone height `>= fvg_min_ticks * tick`.

### 5.3 Displacement
The 3-bar leg is impulsive: `high[t] - low[t-2] >= disp_atr_mult * ATR(atr_len)`
(`disp_atr_mult = 1.5`, `atr_len = 14`). Weak/overlapping legs are rejected.

### 5.4 Break of Structure (BOS / MSS)
- **Bullish BOS:** `close[t] > ` most recent **confirmed** swing high.
- **Bearish BOS:** `close[t] < ` most recent **confirmed** swing low.

### 5.5 Order block / breaker
The structural leg the FVG must overlap:
- **Bullish setup:** the **last down-close candle** in the `ob_lookback = 5` bars
  ending at `t-2` (the block being reclaimed by the up-displacement). Zone =
  `[low, high]` of that candle. (Its reclaim after prior weakness is the
  "failed OB / breaker" essence.)
- **Bearish setup:** the last **up-close** candle in the same window.

### 5.6 Unicorn trigger (bullish; bearish mirrors)
All true at bar `t`:
1. Bullish FVG (5.2) with sufficient height.
2. Displacement (5.3).
3. Bullish BOS (5.4).
4. An order block exists (5.5) and its zone **overlaps** the FVG zone
   (`overlap = [max(lows), min(highs)]` non-empty).
→ **Arm a bullish Unicorn**: resting **buy limit at FVG CE**.

## 6. Entry, stop, target (CE-limit v1)
- **Entry:** resting limit at CE. Filled when a later bar trades to CE
  (long: `low <= CE`). A same-bar limit fill then faces that bar's stop/target
  (no look-ahead free ride). Setup **expires** after `max_age_bars = 20` unfilled,
  or **invalidates** if a bar closes beyond the stop level before fill.
- **Stop:** `min(ob_low, fvg_low) - stop_buffer_ticks * tick` (mirror for shorts).
- **Target:** `target_mode`:
  - `liquidity` (default): nearest **confirmed swing high** above entry (bullish);
    if none within reach, fall back to R-multiple.
  - `r_multiple`: `entry ± target_R * risk_dist` (`target_R = 2.0`).
- **One position at a time**, both directions. `eod_flat` always applies.
- **Intrabar priority:** stop before target (conservative).

## 7. Sizing & costs
Same as ORB: `risk_pct = 1%` of equity, contracts = `floor(risk$/(risk_dist *
$per_point))`; commission + `slippage_ticks = 1`/side; `starting_capital 100k`.

## 8. Parameters (master)
| Param | Default | Meaning |
|---|---|---|
| `exec_tf` | 5m | detection+entry timeframe |
| `swing_n` | 2 | fractal half-width |
| `atr_len` | 14 | ATR for displacement |
| `disp_atr_mult` | 1.5 | displacement strength |
| `fvg_min_ticks` | 4 | min FVG height |
| `ob_lookback` | 5 | bars to find the block |
| `max_age_bars` | 20 | unfilled-setup expiry |
| `stop_buffer_ticks` | 4 | stop beyond zone |
| `target_mode` | liquidity | liquidity \| r_multiple |
| `target_R` | 2.0 | fallback / r_multiple target |
| `direction` | both | both \| long \| short |
| `risk_pct` | 0.01 | equity risked per trade |

## 9. Filters / modules (future toggles — not in v1 baseline)
`session_filter` (killzone), `htf_bias` (only with higher-TF trend),
`require_liquidity_sweep` (reversal flavor), `rejection_confirm` (candle),
`displacement_body_only`.

## 10. Backtest config & acceptance
- ES/NQ, Dukascopy 5m, 2015–2025; IS 2015–2021 / OOS 2022–2025; costs on.
- **Because trades are few**, require `n_trades > 100` before trusting metrics;
  report trades/year. Acceptance (OOS, per instrument): PF > 1.3, Sharpe > 0.8,
  maxDD < 20% — **and** the same definitions must not have been tuned on OOS.

## 11. Failure modes & risks
- **Definition sensitivity** — swing N, disp mult, ob_lookback all change setups.
- **Overfitting / hindsight** — SMC is notorious; keep params few, lean on OOS.
- **Sparse trades** — significance; a few lucky years can dominate.
- **5m single-TF** is a simplification of the HTF→LTF ICT workflow; a real
  divergence from how the model is traded.

## 12. Change Log
- 2026-07-11 — created. v1 = single-TF 5m, both directions, CE-limit entry, ES/NQ.

## 13. Machine Manifest
```yaml
# karmabase-manifest
id: unicorn
family: unicorn
name: ICT Unicorn (Breaker + FVG)
description: Displacement breaks structure, leaves an FVG overlapping an order block; enter at FVG CE.
variants: [both, long, short]
instruments: [ES, NQ]
status: draft
params:
  exec_tf:
    default: "5m"
    choices: ["5m", "1m", "15m"]
    label: Timeframe
  disp_atr_mult:
    default: 1.5
    min: 0.5
    max: 3.0
    step: 0.25
    label: Displacement strength (xATR)
  fvg_min_ticks:
    default: 4
    choices: [0, 4, 8, 12]
    label: Min FVG height (ticks)
  ob_lookback:
    default: 5
    choices: [3, 5, 8]
    label: Order-block lookback (bars)
  max_age_bars:
    default: 20
    choices: [10, 20, 40]
    label: Setup expiry (bars)
  target_mode:
    default: liquidity
    choices: [liquidity, r_multiple]
    label: Target logic
  target_R:
    default: 2.0
    min: 1.0
    max: 4.0
    step: 0.5
    label: Target (R multiple)
```
