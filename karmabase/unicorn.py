"""ICT Unicorn engine (Breaker/OB + FVG overlap, CE-limit entry).

Mechanizes strategies/unicorn.md. Event-driven, per-session, one-position-at-a-time,
no look-ahead:
  - swings are fractals confirmed with an N-bar lag (only used once confirmed);
  - a 3-bar FVG + displacement + break-of-structure + overlapping order block
    arms a resting limit at the FVG CE;
  - a same-bar limit fill still faces that bar's stop/target (no free ride).

This is ONE mechanization of a discretionary concept — see the spec's warnings.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time

import numpy as np
import pandas as pd

from .config import INSTRUMENTS
from .data import sessions
from .engine import Trade, BacktestResult


@dataclass
class UnicornConfig:
    variant: str = "both"               # both | long | short
    instrument: str = "ES"
    exec_tf: str = "5m"

    # session
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    entry_cutoff: time = time(15, 30)
    eod_flat: time = time(15, 55)

    # detection
    swing_n: int = 2
    atr_len: int = 14
    disp_atr_mult: float = 1.5
    fvg_min_ticks: int = 4
    ob_lookback: int = 5
    max_age_bars: int = 20
    stop_buffer_ticks: int = 4

    # target
    target_mode: str = "liquidity"      # liquidity | r_multiple
    target_R: float = 2.0

    # costs / sizing
    commission: float | None = None
    slippage_ticks: int = 1
    risk_pct: float = 0.01
    max_contracts: int = 10
    starting_capital: float = 100_000.0

    def instrument_spec(self):
        return INSTRUMENTS[self.instrument]

    @property
    def direction(self) -> str:
        return self.variant


def preset(variant="both", instrument="ES", **overrides) -> UnicornConfig:
    return replace(UnicornConfig(variant=variant, instrument=instrument), **overrides)


class UnicornBacktester:
    def __init__(self, cfg: UnicornConfig):
        self.cfg = cfg
        self.spec = cfg.instrument_spec()
        self.commission = cfg.commission if cfg.commission is not None else self.spec.commission
        self.tick = self.spec.tick_size

    # -- public ------------------------------------------------------------
    def run(self, df: pd.DataFrame) -> BacktestResult:
        cfg = self.cfg
        equity = cfg.starting_capital
        all_trades, daily_rows = [], []
        n_days = skipped = 0
        for day, g in sessions(df):
            n_days += 1
            if len(g) < cfg.atr_len + 2 * cfg.swing_n + 3:
                skipped += 1
                continue
            trades, equity = self._run_session(day, g, equity)
            all_trades.extend(trades)
            daily_rows.append({"session": day, "trades": len(trades),
                               "pnl": sum(t.pnl for t in trades), "equity": equity})
        trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
        return BacktestResult(trades_df, pd.DataFrame(daily_rows), cfg, n_days, skipped)

    # -- indicators --------------------------------------------------------
    def _prepare(self, g):
        cfg = self.cfg
        h, l, c = g.high.to_numpy(), g.low.to_numpy(), g.close.to_numpy()
        o = g.open.to_numpy()
        prev_c = np.concatenate([[c[0]], c[:-1]])
        tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
        atr = pd.Series(tr).rolling(cfg.atr_len, min_periods=1).mean().to_numpy()
        n = cfg.swing_n
        N = len(h)
        sh = np.zeros(N, bool)
        sl = np.zeros(N, bool)
        for s in range(n, N - n):
            if h[s] == h[s - n:s + n + 1].max() and h[s] > h[s - 1] and h[s] > h[s + 1]:
                sh[s] = True
            if l[s] == l[s - n:s + n + 1].min() and l[s] < l[s - 1] and l[s] < l[s + 1]:
                sl[s] = True
        return dict(o=o, h=h, l=l, c=c, atr=atr, sh=sh, sl=sl,
                    idx=g.index)

    # -- session loop ------------------------------------------------------
    def _run_session(self, day, g, equity):
        cfg, tick = self.cfg, self.tick
        d = self._prepare(g)
        o, h, l, c, atr, sh, sl, idx = (d[k] for k in
                                        ("o", "h", "l", "c", "atr", "sh", "sl", "idx"))
        N = len(c)
        n = cfg.swing_n
        buf = cfg.stop_buffer_ticks * tick
        fvg_min = cfg.fvg_min_ticks * tick

        conf_high, conf_low = [], []          # confirmed swing prices (as of bar i)
        last_sh = last_sl = None
        pos = None
        setup = None                          # one armed resting limit at a time
        trades = []

        for i in range(N):
            ts = idx[i]
            t = ts.time()

            # confirm swings with N-bar lag
            s = i - n
            if s >= 0:
                if sh[s]:
                    conf_high.append(h[s]); last_sh = h[s]
                if sl[s]:
                    conf_low.append(l[s]); last_sl = l[s]

            # EOD flat
            if pos is not None and t >= cfg.eod_flat:
                pos, tr = self._close(pos, o[i], ts, "eod", market=True)
                equity += tr.pnl; trades.append(tr)
            if t >= cfg.eod_flat:
                setup = None
                continue

            # manage open position
            if pos is not None:
                ex = self._intrabar_exit(pos, o[i], h[i], l[i])
                if ex is not None:
                    price, reason, market = ex
                    pos, tr = self._close(pos, price, ts, reason, market)
                    equity += tr.pnl; trades.append(tr)

            # manage / fill an armed setup
            if pos is None and setup is not None:
                if i > setup["expire"] or self._invalidated(setup, c[i]):
                    setup = None
                elif self._touched(setup, h[i], l[i]):
                    pos = self._open(setup, ts, equity)
                    setup = None
                    if pos is not None:
                        ex = self._same_bar_exit(pos, h[i], l[i])
                        if ex is not None:
                            price, reason, market = ex
                            pos, tr = self._close(pos, price, ts, reason, market)
                            equity += tr.pnl; trades.append(tr)

            # detect a new Unicorn (flat, no armed setup, before cutoff)
            if pos is None and setup is None and t < cfg.entry_cutoff and i >= 2:
                setup = self._detect(i, o, h, l, c, atr, last_sh, last_sl,
                                     conf_high, conf_low, fvg_min, buf)

        # safety close
        if pos is not None:
            pos, tr = self._close(pos, c[-1], idx[-1], "eod", market=True)
            equity += tr.pnl; trades.append(tr)
        return trades, equity

    # -- detection ---------------------------------------------------------
    def _detect(self, i, o, h, l, c, atr, last_sh, last_sl, conf_high, conf_low,
                fvg_min, buf):
        cfg = self.cfg
        leg_range = max(h[i], h[i - 1], h[i - 2]) - min(l[i], l[i - 1], l[i - 2])
        if leg_range < cfg.disp_atr_mult * atr[i]:
            return None
        allow_long = cfg.direction in ("both", "long")
        allow_short = cfg.direction in ("both", "short")

        # --- bullish: FVG low[i] > high[i-2], BOS up, down-close OB overlap ---
        if allow_long and l[i] > h[i - 2] and (l[i] - h[i - 2]) >= fvg_min \
                and last_sh is not None and c[i] > last_sh:
            fvg_lo, fvg_hi = h[i - 2], l[i]
            ob = self._find_ob(i, o, c, h, l, want_down=True)
            if ob is not None and max(fvg_lo, ob[0]) <= min(fvg_hi, ob[1]):
                ce = (fvg_lo + fvg_hi) / 2
                stop = min(ob[0], fvg_lo) - buf
                if ce - stop > 0:
                    tgt = self._target("long", ce, ce - stop, conf_high)
                    return {"side": "long", "ce": ce, "stop": stop, "target": tgt,
                            "expire": i + cfg.max_age_bars,
                            "fvg": (fvg_lo, fvg_hi), "ob": ob}

        # --- bearish mirror ---
        if allow_short and h[i] < l[i - 2] and (l[i - 2] - h[i]) >= fvg_min \
                and last_sl is not None and c[i] < last_sl:
            fvg_hi, fvg_lo = l[i - 2], h[i]
            ob = self._find_ob(i, o, c, h, l, want_down=False)
            if ob is not None and max(fvg_lo, ob[0]) <= min(fvg_hi, ob[1]):
                ce = (fvg_lo + fvg_hi) / 2
                stop = max(ob[1], fvg_hi) + buf
                if stop - ce > 0:
                    tgt = self._target("short", ce, stop - ce, conf_low)
                    return {"side": "short", "ce": ce, "stop": stop, "target": tgt,
                            "expire": i + cfg.max_age_bars,
                            "fvg": (fvg_lo, fvg_hi), "ob": ob}
        return None

    def _find_ob(self, i, o, c, h, l, want_down: bool):
        """Last down-close (want_down) / up-close candle within ob_lookback ending at i-2."""
        for k in range(i - 2, max(i - 2 - self.cfg.ob_lookback, -1), -1):
            down = c[k] < o[k]
            if down == want_down:
                return (l[k], h[k])
        return None

    def _target(self, side, ce, risk, conf):
        cfg = self.cfg
        if cfg.target_mode == "liquidity":
            if side == "long":
                cand = [p for p in conf if p >= ce + risk]   # >= ~1R away, above
                if cand:
                    return min(cand)
            else:
                cand = [p for p in conf if p <= ce - risk]
                if cand:
                    return max(cand)
        # r_multiple / fallback
        return ce + cfg.target_R * risk if side == "long" else ce - cfg.target_R * risk

    # -- fills / exits -----------------------------------------------------
    def _touched(self, setup, hi, lo):
        return lo <= setup["ce"] if setup["side"] == "long" else hi >= setup["ce"]

    def _invalidated(self, setup, close):
        return close < setup["stop"] if setup["side"] == "long" else close > setup["stop"]

    def _open(self, setup, ts, equity):
        cfg, spec = self.cfg, self.spec
        entry = setup["ce"]                     # limit fill at CE (no slippage)
        stop, target = setup["stop"], setup["target"]
        r_dist = abs(entry - stop)
        if r_dist <= 0:
            return None
        contracts = int(min(max(np.floor(equity * cfg.risk_pct /
                                          (r_dist * spec.point_value)), 0), cfg.max_contracts))
        if contracts <= 0:
            return None
        p = Trade(session=ts.date(), side=setup["side"], entry_time=ts, entry_price=entry,
                  stop=stop, target=target, contracts=contracts, r_dist=r_dist)
        return p

    def _intrabar_exit(self, pos, o, h, l):
        slip = self.cfg.slippage_ticks * self.tick
        if pos.side == "long":
            if o <= pos.stop:
                return (o - slip, "stop", True)
            if l <= pos.stop:
                return (pos.stop - slip, "stop", True)
            if pos.target is not None and h >= pos.target:
                return (pos.target, "target", False)
        else:
            if o >= pos.stop:
                return (o + slip, "stop", True)
            if h >= pos.stop:
                return (pos.stop + slip, "stop", True)
            if pos.target is not None and l <= pos.target:
                return (pos.target, "target", False)
        return None

    def _same_bar_exit(self, pos, h, l):
        if pos.side == "long":
            if l <= pos.stop:
                return (pos.stop, "stop", True)
            if pos.target is not None and h >= pos.target:
                return (pos.target, "target", False)
        else:
            if h >= pos.stop:
                return (pos.stop, "stop", True)
            if pos.target is not None and l <= pos.target:
                return (pos.target, "target", False)
        return None

    def _close(self, pos, price, ts, reason, market):
        cfg, spec = self.cfg, self.spec
        slip = cfg.slippage_ticks * self.tick if market else 0.0
        exit_px = price - slip if pos.side == "long" else price + slip
        sign = 1 if pos.side == "long" else -1
        gross = sign * (exit_px - pos.entry_price) * spec.point_value * pos.contracts
        pos.exit_time = ts
        pos.exit_price = exit_px
        pos.reason = reason
        pos.pnl = gross - 2 * self.commission * pos.contracts
        pos.r_multiple = sign * (exit_px - pos.entry_price) / pos.r_dist
        return None, pos
