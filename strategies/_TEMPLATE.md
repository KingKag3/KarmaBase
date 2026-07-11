# Strategy: <NAME>

**ID:** `<short-slug>`
**Status:** draft | backtesting | validated | live | retired
**Author:** kingkag3
**Created:** <YYYY-MM-DD>

---

## 1. Thesis
> Why does this edge exist? Behavioral, structural, or statistical rationale.
> What inefficiency are we harvesting, and why should it persist?

## 2. Universe
- **Asset class:** <crypto / equities / futures / fx>
- **Instruments:** <e.g. BTC/USDT, SPY, ES>
- **Liquidity / eligibility filters:** <min volume, price, etc.>

## 3. Timeframe
- **Bar frequency:** <1d / 1h / 15m / ...>
- **Session:** <24/7 / RTH / ...>
- **Typical holding period:** <intraday / days / weeks>

## 4. Data
| Field | Source | Notes |
|-------|--------|-------|
| OHLCV | <exchange/provider> | |
| <other> | | |

## 5. Signal Logic  *(the core — must be fully mechanical)*
**State:** long / short / flat (or continuous weight in [-1, 1])

### Entry
- **Long when:** <precise, quantified conditions>
- **Short when:** <precise, quantified conditions>

### Exit
- **Take profit:** <rule>
- **Stop loss:** <rule>
- **Time / condition exit:** <rule>

### Filters / regime gates
- <e.g. only when 200d slope > 0, vol below threshold>

## 6. Sizing & Risk
- **Position sizing:** <fixed fraction / vol target / Kelly fraction>
- **Max position:** <% of capital>
- **Max concurrent exposure:** <...>
- **Leverage:** <...>

## 7. Parameters
| Param | Default | Range (for optimization) | Meaning |
|-------|---------|--------------------------|---------|
| `lookback` | | | |
| `threshold` | | | |

## 8. Cost Assumptions
- **Fees:** <bps per side>
- **Slippage:** <bps / model>
- **Borrow / funding:** <if short or perp>

## 9. Backtest Config
- **Period:** <start – end>
- **In-sample / out-of-sample split:** <...>
- **Starting capital:** <...>
- **Rebalance:** <on signal / periodic>

## 10. Metrics & Acceptance Criteria
| Metric | Target |
|--------|--------|
| Sharpe (net) | > 1.0 |
| Max drawdown | < 20% |
| Win rate | |
| Profit factor | |
| # trades (significance) | |

## 11. Failure Modes & Risks
- <Regimes where this breaks, tail risks, capacity limits, overfitting concerns>

## 12. Notes / Change Log
- <YYYY-MM-DD> — created.
