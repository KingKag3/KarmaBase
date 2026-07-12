"""Performance metrics for a BacktestResult (net of costs)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestResult

TRADING_DAYS = 252


def compute(result: BacktestResult) -> dict:
    tr = result.trades
    daily = result.daily.copy()
    cap = result.config.starting_capital

    m: dict = {
        "variant": result.config.variant,
        "instrument": result.config.instrument,
        "n_days": result.n_days,
        "skipped_days": result.skipped_days,
        "n_trades": 0,
    }
    if tr.empty:
        return m

    wins = tr[tr.pnl > 0]
    losses = tr[tr.pnl <= 0]
    gross_win = wins.pnl.sum()
    gross_loss = -losses.pnl.sum()

    m.update({
        "n_trades": len(tr),
        "win_rate": len(wins) / len(tr),
        "avg_R": tr.r_multiple.mean(),
        "expectancy_R": tr.r_multiple.mean(),      # R per trade
        "avg_win": wins.pnl.mean() if len(wins) else 0.0,
        "avg_loss": losses.pnl.mean() if len(losses) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else np.inf,
        "total_pnl": tr.pnl.sum(),
        "total_return_pct": tr.pnl.sum() / cap * 100,
        "avg_trades_per_day": len(tr) / max(result.n_days - result.skipped_days, 1),
        "exit_mix": tr.reason.value_counts().to_dict(),
    })

    # equity-curve / risk metrics from daily pnl
    if not daily.empty:
        eq = daily.equity.to_numpy()
        prev = np.concatenate([[cap], eq[:-1]])
        rets = daily.pnl.to_numpy() / prev
        sharpe = (rets.mean() / rets.std() * np.sqrt(TRADING_DAYS)
                  if rets.std() > 0 else 0.0)
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        m.update({
            "sharpe": sharpe,
            "max_drawdown_pct": dd.min() * 100,
            "final_equity": eq[-1],
        })
    return m


def summary_line(m: dict) -> str:
    if m.get("n_trades", 0) == 0:
        return f"{m['variant']:<8} {m['instrument']:<4}  NO TRADES ({m['n_days']} days)"
    return (
        f"{m['variant']:<8} {m['instrument']:<4} "
        f"trades={m['n_trades']:>4}  "
        f"win={m['win_rate']*100:>5.1f}%  "
        f"avgR={m['avg_R']:>+5.2f}  "
        f"PF={m['profit_factor']:>4.2f}  "
        f"Sharpe={m.get('sharpe',0):>+5.2f}  "
        f"maxDD={m.get('max_drawdown_pct',0):>6.1f}%  "
        f"ret={m['total_return_pct']:>+6.2f}%"
    )
