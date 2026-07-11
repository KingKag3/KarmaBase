"""Run the ORB backtest.

Prototype driver: pulls free SPY/QQQ intraday from yfinance and runs the three
baseline variants (classic / vwap / retest). This validates ENGINE MECHANICS
and gives preliminary numbers only — see orb.md §4/§12 (short history; real
ES/NQ data needed for final validation).

Usage:
    python -m backtests.run_orb                      # SPY, all variants, 5m/60d
    python -m backtests.run_orb --instrument QQQ
    python -m backtests.run_orb --interval 1m --period 7d
    python -m backtests.run_orb --variant classic
"""
from __future__ import annotations

import argparse
import sys

from kquant import config as cfgmod
from kquant.data import load_intraday
from kquant.engine import ORBBacktester
from kquant.metrics import compute, summary_line


def main(argv=None):
    ap = argparse.ArgumentParser(description="ORB backtest (prototype on SPY/QQQ).")
    ap.add_argument("--instrument", default="SPY", choices=list(cfgmod.INSTRUMENTS))
    ap.add_argument("--variant", default="all",
                    choices=["all", "classic", "vwap", "retest"])
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default=None)
    ap.add_argument("--target-r", type=float, default=1.0)
    args = ap.parse_args(argv)

    spec = cfgmod.INSTRUMENTS[args.instrument]
    print(f"Loading {spec.yf_symbol} {args.interval} bars "
          f"(period={args.period or 'default'})...")
    df = load_intraday(spec.yf_symbol, interval=args.interval, period=args.period)
    print(f"  {len(df):,} bars  {df.index.min()}  ->  {df.index.max()}\n")

    variants = ["classic", "vwap", "retest"] if args.variant == "all" else [args.variant]
    print(f"{'variant':<8} {'sym':<4} results (net of costs, exec_tf={args.interval})")
    print("-" * 92)
    results = {}
    for v in variants:
        cfg = cfgmod.PRESETS[v](args.instrument, exec_tf=args.interval,
                                target_R=args.target_r)
        res = ORBBacktester(cfg).run(df)
        m = compute(res)
        results[v] = (res, m)
        print(summary_line(m))

    print("-" * 92)
    # exit-reason breakdown for the classic run (sanity of mechanics)
    if "classic" in results:
        _, m = results["classic"]
        if m.get("n_trades"):
            print(f"classic exit mix: {m['exit_mix']}")
            print(f"classic avg trades/day: {m['avg_trades_per_day']:.2f}   "
                  f"skipped (no OR): {m['skipped_days']}/{m['n_days']} days")
    return results


if __name__ == "__main__":
    sys.exit(0 if main() else 0)
