# Strategy: Opening Range Breakout (ORB) — ES/NQ Index Futures

**ID:** `orb`
**Status:** draft
**Author:** kingkag3
**Created:** 2026-07-11

This spec defines **one configurable ORB engine** and three concrete baseline
variants built on it:

| Variant | ID | One-liner |
|---|---|---|
| A — Classic ORB | `orb.classic` | First exec-bar **close** beyond the opening range → enter breakout direction. |
| B — VWAP-Filtered ORB | `orb.vwap` | Classic, but only take longs **above** VWAP and shorts **below** VWAP. |
| C — Breakout-Retest ORB | `orb.retest` | Wait for the break, then enter on the **pullback/retest** of the broken level. |

Variants A and B share identical mechanics except for the VWAP gate. Variant C
changes the **entry model** only; range definition, risk, and exits are shared.

> **Design principle:** the ~15 ORB "variants" in the source framework are not
> separate strategies — they are **toggleable modules** on this one engine
> (see §9 *Variant Taxonomy*). We build the three baselines first; every other
> flavor is a config preset we can sweep and A/B against the baseline later.

---

## 1. Thesis
The cash-session open concentrates overnight information, order imbalances, and
the highest intraday participation into the first minutes of trade. The
**opening range** (first 15 min) frames the market's initial agreed value. A
decisive break of that range signals that one side has won the auction, and
momentum/continuation tends to follow on trending, high-volatility days. The
edge is **structural** (liquidity/participation clustering at the open) plus
**behavioral** (breakout chasing, stop runs above/below the range).

The edge **decays in chop**: on low-volatility, mean-reverting days near major
levels, breakouts fail and the range gets whipsawed. Filters (VWAP, volume,
momentum) and a re-arm rule exist to reduce those false-break trades.

---

## 2. Universe
- **Asset class:** CME index futures.
- **Instruments:** ES (E-mini S&P 500) and NQ (E-mini Nasdaq-100). Baseline
  runs each instrument independently (no cross-hedging in v1).
- **Contract:** front-month, roll on volume (back-adjusted continuous series
  for backtest; see §7 Data).
- **Point values / ticks:**
  | | Tick | $/tick | $/point |
  |---|---|---|---|
  | ES | 0.25 | $12.50 | $50 |
  | NQ | 0.25 | $5.00 | $20 |
- **Eligibility:** trade every regular session; optional skip of half-days and
  major scheduled events (FOMC, CPI, NFP) — off by default, param `skip_events`.

---

## 3. Timeframe & Session
- **Timezone:** `America/New_York` (DST-aware). All session logic uses ET.
- **Opening range window:** `09:30:00`–`09:45:00` ET (15 min) → param `or_minutes = 15`.
- **Execution bars:** `exec_tf = 5m` (default). With 5-min bars the OR is the
  three bars stamped 09:30, 09:35, 09:40 (covering 09:30–09:45). Alt: `1m`.
- **First eligible entry bar:** the bar that closes at/after `09:45` ET.
- **No-new-entry cutoff:** `entry_cutoff = 15:30 ET` (no fresh breakouts after this).
- **Hard flat (EOD):** `eod_flat = 15:55 ET` — all positions force-closed at the
  open of the 15:55 bar (index futures liquidity thins into the 16:00 cash close).
- **Holding period:** intraday only, **no overnight positions**, and in practice
  **most trades resolve in under ~10 minutes** (fast stop/target/time-stop hits).
- **⚠️ Bar-resolution requirement:** because typical holds are ~10 min, the
  execution bar must be **much finer** than the trade duration or the backtest is
  guessing the intrabar path. On `5m` bars a 10-min trade is only ~2 bars, so the
  conservative stop-first assumption dominates and results are unreliable.
  **Validate on `exec_tf = 1m`** (≈10 bars/trade); treat 5m as a rough prototype only.

---

## 4. Data
| Field | Granularity | Source (see notes) |
|-------|-------------|--------------------|
| OHLCV | `exec_tf` (1m/5m) bars, RTH + a few pre/post bars for VWAP anchor | Databento / IBKR / CME (real ES·NQ) |
| Session calendar | daily | exchange calendar (`pandas_market_calendars`) |
| Contract roll dates | per roll | data vendor |

**Data source decision (OPEN — see §12):** true ES/NQ intraday is paid
(Databento, FirstRate, IBKR). For **prototyping only** we may proxy with **SPY /
QQQ** 1-min bars (free-ish), accepting that ETF RTH ≠ futures 24h session and
that overnight gaps land differently. Proxy is for wiring the engine, **not** for
final validation. Baseline validation must use real futures data.

---

## 5. Shared Engine Mechanics (all variants)

### 5.1 Opening range
For each session:
```
OR_high = max(high) over bars in [09:30, 09:45) ET
OR_low  = min(low)  over bars in [09:30, 09:45) ET
OR_mid  = (OR_high + OR_low) / 2
OR_range = OR_high - OR_low            # "range height", in points
```
Skip the session if `OR_range < min_range_ticks * tick` (degenerate/thin range;
param `min_range_ticks = 4`).

### 5.2 Breakout detection (close confirmation)
Using **execution-bar closes** (wick pokes do **not** count — per framework):
- **Long break:** an exec bar closes `> OR_high` (strictly, plus optional
  `break_buffer_ticks = 0`).
- **Short break:** an exec bar closes `< OR_low`.

### 5.3 Fill model (no look-ahead)
The bar whose close triggers the signal is the **signal bar**. Entry is filled at
the **open of the next exec bar**, plus slippage (§5.7). This prevents using the
signal bar's own close as a fill price. (Alt aggressive model `fill_on_close`
available for sensitivity checks; off by default.)

### 5.4 Re-arm rule (controls "unlimited" re-entries)
Direction is **both sides, unlimited**, but to avoid machine-gunning the same
level:
- After any position (long or short) **closes**, that side is **disarmed**.
- A side **re-arms** only after price trades **back inside** the range
  (long side re-arms once a bar closes `<= OR_high`; short side once a bar
  closes `>= OR_low`).
- A re-armed side can then take a **fresh** breakout close.
- Cap: `max_trades_per_side = ∞` (unlimited) but bounded in practice by
  `entry_cutoff` and re-arm. Optional hard cap `max_trades_day` (default off).

### 5.5 Stops
`stop_mode` (default `opposite_range`):
- `opposite_range`: long stop = `OR_low`; short stop = `OR_high`.
- `range_mid`: stop = `OR_mid` (tighter, more stop-outs).
- `atr`: stop = entry ∓ `atr_mult * ATR(atr_len)` on `exec_tf`
  (`atr_len = 14`, `atr_mult = 1.0`).
- `fixed_ticks`: stop = entry ∓ `stop_ticks` (default 40 ES / 60 NQ ticks).

Define per-trade risk distance `R_dist = |entry - stop|` (points).

### 5.6 Targets & exits
`exit_mode` (default `r_multiple`):
- `r_multiple`: take-profit at `entry ± target_R * R_dist` (`target_R = 1.0`
  baseline; also test 1.5, 2.0).
- `measured_move`: take-profit at `entry ± OR_range` (project range height).
- `trail`: trail a stop at `trail_atr_mult * ATR` below/above running extreme
  once trade is `>= trail_arm_R` in profit (`trail_arm_R = 1.0`,
  `trail_atr_mult = 1.5`).
- `time_stop` (combinable flag `use_time_stop`, default **on**): if price
  **re-enters the opposite side of the range** (long: a bar closes `< OR_high`;
  i.e. back inside) within `time_stop_bars` exec bars of entry and trade is
  not yet `>= 0.5R` in profit, exit at next bar open. Captures "failed ORB
  reverses quickly." **`time_stop_bars` is in BARS, so it scales with `exec_tf`:**
  on 5m, `6` = 30 min (too loose given ~10-min holds); on 1m use ≈`10` (10 min).
- **Partial scaling** (optional `scale_out`, default off): take 50% at 1R, trail
  remainder.
- **EOD flat** (§3) always applies.
- **Intrabar priority:** if a single bar's range spans **both** stop and target,
  assume **stop hit first** (conservative).

### 5.7 Costs
- **Commission:** `commission = $2.50 per side` (`$5 round-trip` per contract).
- **Slippage:** `slippage_ticks = 1` per side (entry and exit each), applied
  adverse to fills.
- **Funding/borrow:** n/a (futures).

### 5.8 Position sizing
`sizing = risk_pct` (default):
```
risk_$        = equity * risk_pct          # risk_pct = 1.0%
contracts     = floor( risk_$ / (R_dist * $per_point) )
contracts     = max(contracts, 0)          # 0 → skip trade if too tight/rich
```
- `starting_capital = 100_000`.
- Alt `sizing = fixed_contracts` (`n = 1`) for clean per-trade comparison.
- `max_contracts` cap default 10.

---

## 6. Variant A — Classic ORB  (`orb.classic`)
The control. Uses shared mechanics with:
- Entry: §5.2 breakout **close** → §5.3 next-bar-open fill.
- No directional filter (take both sides as they break, subject to re-arm).
- `stop_mode = opposite_range`, `exit_mode = r_multiple`, `target_R = 1.0`,
  `use_time_stop = true`.

**This is the benchmark every other variant is measured against.**

---

## 7. Variant B — VWAP-Filtered ORB  (`orb.vwap`)
Classic ORB **plus** a VWAP directional gate.

### 7.1 VWAP definition
Session-anchored VWAP, reset at 09:30 ET:
```
TP_t   = (high_t + low_t + close_t) / 3
VWAP_t = cumsum(TP_t * volume_t) / cumsum(volume_t)     # cumulative from 09:30
```
### 7.2 Gate (evaluated on the signal bar)
- **Long allowed** only if `close_signal > VWAP` (and, if `use_vwap_slope`,
  `VWAP_t - VWAP_{t-k} > 0`).
- **Short allowed** only if `close_signal < VWAP` (slope `< 0`).
- `vwap_slope_lookback k = 3` exec bars; `use_vwap_slope = false` by default
  (test both).
Everything else identical to Variant A.

---

## 8. Variant C — Breakout-Retest ORB  (`orb.retest`)
Same range/risk/exits; **different entry model** — enter on the pullback to the
broken level instead of chasing the breakout close.

### 8.1 Sequence (long side; short is mirror)
1. **Arm:** an exec bar closes `> OR_high` (the breakout). Record `break_bar`.
2. **Wait for retest** within `retest_window = 8` exec bars:
   - Retest occurs when a subsequent bar's **low `<= OR_high + retest_buffer`**
     (`retest_buffer = 2 ticks`), i.e. price pulls back to the broken level.
3. **Entry trigger** (`retest_confirm` mode):
   - `touch` (default): fill a **limit** at `OR_high` on the retest tap.
   - `reject` (the "retest-and-rejection" flavor): require the retest bar to
     **close back above `OR_high`** after tagging it, then fill at next bar open.
     More confirmation, fewer fills.
4. **Invalidate** (no trade) if, before entry, a bar **closes `< OR_low`**
   (break failed) or the `retest_window` elapses with no qualifying retest.
5. **Stop:** `stop_mode = opposite_range` (=`OR_low`) by default; tighter option
   `retest_swing`: stop just below the retest bar's low.
6. Exits/targets/costs/sizing per §5.

### 8.2 Notes
- Retest gives **tighter R_dist** (entry nearer the level) → larger size for the
  same risk %, but **more missed trades** (breakout runs without pulling back).
  We expect fewer trades, higher avg R, lower hit-rate-of-participation. The
  backtest must log **missed retests** to quantify opportunity cost vs. Classic.

---

## 9. Variant Taxonomy (future modules — quantized, not in baseline)
Documented now so the engine's parameter surface is designed to accept them.
Each is a flag/preset layered on the base:

| Module | Flag | Quantified rule (default) |
|---|---|---|
| Volume-confirmed | `filter_volume` | breakout bar `RVOL = vol / SMA(vol, 20) >= 1.5` |
| Momentum (RSI) | `filter_rsi` | long if `RSI(14) > 55`, short if `< 45` on `exec_tf` |
| Strong-close | `filter_strong_close` | long: `close` in top 25% of bar range; short: bottom 25% |
| Multi-timeframe | `mtf` | define OR on 15m, execute/confirm on 1m |
| Gap-and-go | `context=gap_go` | only long if `open_gap >= +0.5 * ATR(14,daily)`; continuation |
| Gap-fade | `context=gap_fade` | fade: short a failed up-gap after OR_low breaks (countertrend) |
| Liquidity-sweep | `context=sweep` | require wick sweep of OR extreme then reclaim (close back inside → break other way) |
| Session-aware | `sessions=[NY]` | restrict to chosen session open(s); NY only in v1 |
| One-trade-per-side | `max_trades_per_side=1` | cap re-entries |
| Strict-risk | preset | `fixed_ticks` stop + `target_R` fixed + `daily_loss_limit` |
| Measured-move | `exit_mode=measured_move` | target = ±`OR_range` |
| Time-stop | `use_time_stop` | §5.6 (on by default in baselines) |

Also planned risk governor: `daily_loss_limit = 2%` of equity (halt new entries
for the day when breached) — off in baseline, on in the `strict-risk` preset.

---

## 10. Parameters (master list)
| Param | Default | Sweep range | Meaning |
|---|---|---|---|
| `instrument` | ES | {ES, NQ} | contract |
| `exec_tf` | 5m | {1m, 5m} | execution bar size |
| `or_minutes` | 15 | {5,15,30,60} | opening-range length |
| `entry_cutoff` | 15:30 | — | last time to open a trade |
| `eod_flat` | 15:55 | — | force-flat time |
| `min_range_ticks` | 4 | {0,4,8} | skip degenerate ranges |
| `break_buffer_ticks` | 0 | {0,1,2} | extra ticks beyond range to confirm |
| `stop_mode` | opposite_range | {opposite_range,range_mid,atr,fixed_ticks} | stop placement |
| `atr_len` / `atr_mult` | 14 / 1.0 | — | for atr stop/trail |
| `exit_mode` | r_multiple | {r_multiple,measured_move,trail} | target logic |
| `target_R` | 1.0 | {1.0,1.5,2.0} | R-multiple target |
| `use_time_stop` | true | {t,f} | failed-break time exit |
| `time_stop_bars` | 6 | {4,6,8} | bars to prove follow-through |
| `sizing` | risk_pct | {risk_pct,fixed_contracts} | position sizing |
| `risk_pct` | 1.0% | {0.5,1.0} | equity risked per trade |
| `max_contracts` | 10 | — | size cap |
| `commission` | $2.50/side | — | cost |
| `slippage_ticks` | 1/side | {0,1,2} | cost |
| **B:** `use_vwap` | (variant) | — | VWAP gate on |
| **B:** `use_vwap_slope` | false | {t,f} | require VWAP slope sign |
| **C:** `retest_window` | 8 | {4,8,12} | bars to allow retest |
| **C:** `retest_buffer` | 2 ticks | {0,2,4} | retest tag tolerance |
| **C:** `retest_confirm` | touch | {touch,reject} | entry confirmation |

---

## 11. Backtest Config & Acceptance

### 11.1 Config
- **Period:** ≥ 8 years of RTH data (target 2015–2025) to span regimes
  (2015–16 chop, 2017 low-vol, 2018/2020/2022 high-vol, 2023–24 trend).
- **Split:** in-sample 2015–2021, **out-of-sample 2022–2025** (never optimized on OOS).
- **Capital:** $100,000; sizing per §5.8.
- **Costs:** always on (§5.7).
- **Runs:** each variant A/B/C on ES and NQ separately → 6 baseline runs, plus a
  `target_R`/`exec_tf` sweep on the in-sample set only.

### 11.2 Metrics (net of costs)
| Metric | Report | Baseline acceptance (per instrument, OOS) |
|---|---|---|
| Net Sharpe (daily) | ✓ | > 0.8 |
| Net profit factor | ✓ | > 1.2 |
| Max drawdown (% equity) | ✓ | < 20% |
| Win rate / avg R | ✓ | report |
| Expectancy (R/trade) | ✓ | > 0.05R |
| # trades | ✓ | > 200 (significance) |
| Avg trades/day | ✓ | report |
| Exposure / time-in-market | ✓ | report |
| **C only:** missed-retest count | ✓ | report (opportunity cost) |

**Comparison discipline:** Variants B and C only "earn their complexity" if they
beat Variant A on **risk-adjusted** terms (Sharpe / MAR), not just raw return.

---

## 12. Open Decisions / Risks
- **[OPEN] Data source** for real ES/NQ intraday (Databento vs IBKR vs vendor);
  SPY/QQQ proxy only for wiring. → blocks final validation.
- **Continuous-contract construction:** back-adjusted vs ratio-adjusted rolls
  change gap behavior at the open; must fix method before validation.
- **Overfitting:** 20+ parameters — restrict sweeps to a few, prefer OOS + walk-
  forward; report parameter-sensitivity heatmaps, not single best config.
- **Fill realism:** next-bar-open fill can be optimistic on fast breaks; stress
  with `slippage_ticks=2` and `fill_on_close` off/on.
- **Event days:** FOMC/CPI opens are outliers; decide keep vs `skip_events`.
- **Regime dependence:** ORB is a volatility/trend strategy; expect flat-to-neg
  performance in low-vol years — judge across the full sample, not cherry-picked.

---

## 13. Change Log
- 2026-07-11 — created. Baseline = ES/NQ, 15-min OR, both sides unlimited (with
  re-arm), three variants (Classic / VWAP / Breakout-Retest).
- 2026-07-11 — real-data validation (Dukascopy 2015-2025). Found & fixed a
  retest look-ahead; baseline ORB has no robust edge (classic/vwap negative,
  retest marginal). Added machine manifest for the GUI.
- 2026-07-11 — noted typical holds < ~10 min → 5m bars too coarse; switch
  validation to 1m bars (≈10 bars/trade) and scale `time_stop_bars` accordingly.

## 14. Machine Manifest
The GUI/registry reads this block to list the strategy and build its controls.
It is the machine-readable companion to the prose spec above.

```yaml
# karmabase-manifest
id: orb
family: orb
name: Opening Range Breakout
description: First move beyond the opening range; classic / VWAP-filtered / retest variants.
variants: [classic, vwap, retest]
instruments: [ES, NQ, SPY, QQQ]
status: validated-no-edge
params:
  or_minutes:
    default: 15
    choices: [5, 15, 30, 60]
    label: Opening range (minutes)
  exec_tf:
    default: "1m"
    choices: ["1m", "5m"]
    label: Execution timeframe
  time_stop_bars:
    default: 10
    choices: [4, 6, 10, 15]
    label: Time-stop (bars; ~min on 1m)
  target_R:
    default: 1.0
    min: 0.5
    max: 3.0
    step: 0.5
    label: Target (R multiple)
  stop_mode:
    default: opposite_range
    choices: [opposite_range, range_mid, atr, fixed_ticks]
    label: Stop placement
  risk_pct:
    default: 0.01
    min: 0.0025
    max: 0.03
    step: 0.0025
    label: Risk per trade (fraction)
  use_time_stop:
    default: true
    label: Time-stop failed breakouts
```
