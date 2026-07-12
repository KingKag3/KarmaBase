"""Live ORB signal generator.

Runs the SAME engine state machine (`ORBBacktester._step`) over the current
session's bars up to `as_of`, then reports the actionable decision for the next
bar. Because it reuses the engine, the live signal is guaranteed consistent with
the backtest (backtest/live parity).

Output states:
  or_forming      opening range not complete yet
  no_trade        OR too tight / skipped this session
  flat_armed      flat; resting breakout triggers given (long/short levels)
  entry_queued    breakout confirmed; enter at next bar open
  in_position     a trade is live; hold + manage (stop/target/time-stop)
  session_closed  past entry cutoff / EOD; done for the day

CLI:
    python -m karmabase.signal --instrument SPY --variant retest
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import ORBConfig, PRESETS, INSTRUMENTS
from .data import load_intraday
from .engine import ORBBacktester, SessionState


@dataclass
class Signal:
    as_of: object
    instrument: str
    variant: str
    session: object
    state: str
    signal: str                       # long | short | flat
    last_price: float
    or_high: float | None = None
    or_low: float | None = None
    vwap: float | None = None
    detail: dict = field(default_factory=dict)
    message: str = ""


class SignalGenerator:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg
        self.eng = ORBBacktester(cfg)
        self.spec = self.eng.spec

    def generate(self, df: pd.DataFrame, as_of=None, equity=None) -> Signal:
        cfg = self.cfg
        equity = equity if equity is not None else cfg.starting_capital
        as_of = as_of if as_of is not None else df.index.max()

        day = as_of.date()
        session = df[(df.index.date == day) & (df.index <= as_of)]
        session = session.between_time(cfg.session_open, cfg.session_close)
        last_price = float(session.close.iloc[-1]) if len(session) else float("nan")

        base = Signal(as_of=as_of, instrument=cfg.instrument, variant=cfg.variant,
                      session=day, state="or_forming", signal="flat",
                      last_price=last_price)
        if len(session) < 2:
            base.message = "No session data yet."
            return base

        g = self.eng._prepare(session)
        base.vwap = float(g.vwap.iloc[-1])

        # opening range must be complete
        or_end = self._or_end_time()
        if session.index[-1].time() < or_end:
            base.message = (f"Opening range still forming (completes at "
                            f"{or_end.strftime('%H:%M')} ET).")
            return base

        or_high, or_low = self.eng._opening_range(g)
        if or_high is None:
            base.state, base.message = "no_trade", "Opening range too tight - skip session."
            return base
        base.or_high, base.or_low = or_high, or_low

        # replay the session through the shared engine step
        st = SessionState(or_high=or_high, or_low=or_low,
                          or_range=or_high - or_low, equity=equity)
        rows = list(self.eng._active_bars(g).itertuples())
        for i, bar in enumerate(rows):
            self.eng._step(st, i, bar)

        last_bar = rows[-1] if rows else None
        atr = float(g.atr.iloc[-1])
        return self._describe(base, st, last_bar, atr, equity, or_high, or_low)

    # ------------------------------------------------------------------
    def _describe(self, sig, st, last_bar, atr, equity, or_high, or_low):
        cfg = self.cfg
        t = last_bar.Index.time() if last_bar is not None else None

        # 1) live position
        if st.pos is not None:
            p = st.pos
            sign = 1 if p.side == "long" else -1
            unreal_r = sign * (sig.last_price - p.entry_price) / p.r_dist
            sig.state, sig.signal = "in_position", p.side
            sig.detail = {
                "entry": round(p.entry_price, 4), "stop": round(p.stop, 4),
                "target": None if p.target is None else round(p.target, 4),
                "contracts": p.contracts, "bars_held": p.bars_in_trade,
                "unrealized_R": round(unreal_r, 2),
            }
            sig.message = (f"HOLD {p.side.upper()} {p.contracts}x from "
                           f"{p.entry_price:.2f} | stop {p.stop:.2f} | "
                           f"target {'trail' if p.target is None else f'{p.target:.2f}'} "
                           f"| {unreal_r:+.2f}R")
            return sig

        # 2) entry queued for next bar open
        if st.pending is not None:
            side = st.pending["side"]
            sig.state, sig.signal = "entry_queued", side
            sig.message = f"ENTER {side.upper()} at next bar open (breakout confirmed)."
            sig.detail = self._order_preview(side, or_high, or_low, atr, equity)
            return sig

        # 3) session over?
        if t is not None and t >= cfg.entry_cutoff:
            sig.state, sig.signal = "session_closed", "flat"
            sig.message = "FLAT - past entry cutoff; no new trades today."
            return sig

        # 4) flat & armed -> resting triggers
        sig.state, sig.signal = "flat_armed", "flat"
        longs = self._order_preview("long", or_high, or_low, atr, equity)
        shorts = self._order_preview("short", or_high, or_low, atr, equity)
        gate = ""
        if cfg.use_vwap:
            gate = (f"  [VWAP {sig.vwap:.2f}: "
                    f"long needs price>VWAP, short needs price<VWAP]")
        if cfg.variant == "retest":
            watch = []
            if st.retest_long is not None:
                watch.append(f"LONG retest watch: pullback to {or_high:.2f}")
            if st.retest_short is not None:
                watch.append(f"SHORT retest watch: pullback to {or_low:.2f}")
            note = " | ".join(watch) if watch else "awaiting breakout, then retest"
            sig.message = (f"FLAT - retest mode. {note}. "
                           f"OR [{or_low:.2f}, {or_high:.2f}], price {sig.last_price:.2f}.")
        else:
            sig.message = (
                f"FLAT - armed{'(L)' if st.armed_long else ''}"
                f"{'(S)' if st.armed_short else ''}. "
                f"LONG if close > {or_high:.2f} | SHORT if close < {or_low:.2f}."
                f"{gate}")
        sig.detail = {"long_setup": longs, "short_setup": shorts,
                      "armed_long": st.armed_long, "armed_short": st.armed_short}
        return sig

    def _order_preview(self, side, or_high, or_low, atr, equity):
        """Levels a trigger would produce (entry≈break level)."""
        entry = or_high if side == "long" else or_low
        stop = self.eng._stop_price(side, entry, or_high, or_low, atr)
        r_dist = abs(entry - stop)
        target = self.eng._target_price(side, entry, r_dist, or_high - or_low)
        size = self.eng._size(equity, r_dist) if r_dist > 0 else 0
        return {
            "trigger": f"close {'>' if side == 'long' else '<'} {entry:.2f}",
            "approx_entry": round(entry, 4),
            "stop": round(stop, 4),
            "target": None if target is None else round(target, 4),
            "risk_pts": round(r_dist, 4),
            "est_size": size,
        }

    def _or_end_time(self):
        from datetime import time
        o = self.cfg.session_open
        m = o.hour * 60 + o.minute + self.cfg.or_minutes
        return time(m // 60, m % 60)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live ORB signal (prototype on SPY/QQQ).")
    ap.add_argument("--instrument", default="SPY", choices=list(INSTRUMENTS))
    ap.add_argument("--variant", default="retest", choices=["classic", "vwap", "retest"])
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--equity", type=float, default=None)
    args = ap.parse_args(argv)

    spec = INSTRUMENTS[args.instrument]
    df = load_intraday(spec.yf_symbol, interval=args.interval)
    cfg = PRESETS[args.variant](args.instrument, exec_tf=args.interval)
    sig = SignalGenerator(cfg).generate(df, equity=args.equity)

    print(f"\n=== ORB SIGNAL  {sig.instrument}/{sig.variant}  "
          f"session {sig.session} @ {sig.as_of} ===")
    print(f"state   : {sig.state}")
    print(f"signal  : {sig.signal.upper()}")
    print(f"price   : {sig.last_price:.2f}"
          + (f"   OR [{sig.or_low:.2f}, {sig.or_high:.2f}]   VWAP {sig.vwap:.2f}"
             if sig.or_high else ""))
    print(f">> {sig.message}")
    if sig.detail:
        import json
        print("detail  :", json.dumps(sig.detail, default=str))
    return sig


if __name__ == "__main__":
    main()
