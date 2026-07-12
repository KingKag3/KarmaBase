"""ORB real-data validation on Dukascopy index-CFD data (ES/NQ proxies).

Runs the three baseline variants over a multi-year sample with the honest
research protocol from orb.md §11:
  - full sample
  - in-sample (default 2015-2021)  vs  out-of-sample (2022-2025)
  - per-year breakdown (regime dependence)

Usage:
    python -m backtests.validate_orb --instrument ES
    python -m backtests.validate_orb --instrument NQ --is-end 2020 --oos-start 2021
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from karmabase import config as cfgmod
from karmabase.data import load_dukascopy
from karmabase.engine import ORBBacktester
from karmabase.metrics import compute, summary_line


def _slice(df, y0, y1):
    return df[(df.index.year >= y0) & (df.index.year <= y1)]


def _cagr(m, years):
    if not m.get("n_trades") or years <= 0:
        return 0.0
    growth = m["final_equity"] / (m["final_equity"] - m["total_pnl"])
    return (growth ** (1 / years) - 1) * 100 if growth > 0 else 0.0


def run_block(title, df, instrument, variants, target_r, interval, time_stop):
    print(f"\n{title}")
    print("-" * 96)
    out = {}
    y0, y1 = df.index.year.min(), df.index.year.max()
    years = max((df.index.max() - df.index.min()).days / 365.25, 0.1)
    for v in variants:
        cfg = cfgmod.PRESETS[v](instrument, exec_tf=interval, target_R=target_r,
                                time_stop_bars=time_stop)
        res = ORBBacktester(cfg).run(df)
        m = compute(res)
        out[v] = m
        line = summary_line(m)
        if m.get("n_trades"):
            line += f"  CAGR={_cagr(m, years):>+6.2f}%"
        print(line)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ORB real-data validation (Dukascopy).")
    ap.add_argument("--instrument", default="ES", choices=["ES", "NQ"])
    ap.add_argument("--start", type=int, default=2015)
    ap.add_argument("--end", type=int, default=2025)
    ap.add_argument("--is-end", type=int, default=2021, help="last in-sample year")
    ap.add_argument("--oos-start", type=int, default=2022, help="first OOS year")
    ap.add_argument("--target-r", type=float, default=1.0)
    ap.add_argument("--interval", default="1m", choices=["1m", "5m"],
                    help="execution bar size (1m is faithful; ~10-bar trades)")
    ap.add_argument("--variant", default="all",
                    choices=["all", "classic", "vwap", "retest"])
    args = ap.parse_args(argv)

    # time-stop in bars, scaled to bar size (~10 min): 1m->10, 5m->2
    time_stop = 10 if args.interval == "1m" else 2

    spec = cfgmod.INSTRUMENTS[args.instrument]
    print(f"=== ORB VALIDATION: {args.instrument} "
          f"(Dukascopy {spec.duka}, {args.interval}, target_R={args.target_r}, "
          f"time_stop={time_stop}b) ===")
    print(f"Loading {args.start}-{args.end}...")
    df = load_dukascopy(spec.duka, args.interval, args.start, args.end)
    ndays = df.between_time("09:30", "16:00").groupby(df.between_time(
        "09:30", "16:00").index.date).ngroups
    print(f"{len(df):,} bars | {ndays} RTH days | "
          f"{df.index.min().date()} -> {df.index.max().date()}")

    variants = ["classic", "vwap", "retest"] if args.variant == "all" else [args.variant]

    run_block("FULL SAMPLE", df, args.instrument, variants, args.target_r,
              args.interval, time_stop)
    run_block(f"IN-SAMPLE {args.start}-{args.is_end}",
              _slice(df, args.start, args.is_end), args.instrument, variants,
              args.target_r, args.interval, time_stop)
    oos = run_block(f"OUT-OF-SAMPLE {args.oos_start}-{args.end}",
                    _slice(df, args.oos_start, args.end), args.instrument, variants,
                    args.target_r, args.interval, time_stop)

    # per-year breakdown for the best OOS variant by Sharpe
    best = max(variants, key=lambda v: oos[v].get("sharpe", -99) if oos[v].get("n_trades") else -99)
    print(f"\nPER-YEAR breakdown  [{best}]  (regime dependence)")
    print("-" * 96)
    for y in range(args.start, args.end + 1):
        sub = _slice(df, y, y)
        if len(sub) == 0:
            continue
        cfg = cfgmod.PRESETS[best](args.instrument, exec_tf=args.interval,
                                   target_R=args.target_r, time_stop_bars=time_stop)
        m = compute(ORBBacktester(cfg).run(sub))
        if m.get("n_trades"):
            print(f"  {y}  " + summary_line(m).split(None, 2)[2])
        else:
            print(f"  {y}  no trades")


if __name__ == "__main__":
    main()
