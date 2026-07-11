"""ORB backtest engine.

Event-driven, one-position-at-a-time state machine run per session. Implements
the shared mechanics from strategies/orb.md §5 plus the three variants:
  classic (§6), vwap (§7), retest (§8).

No look-ahead: breakout signals on a bar close are filled at the *next* bar open
(unless cfg.fill_on_close). Intrabar, stop is assumed hit before target (§5.6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from .config import ORBConfig
from .data import sessions


@dataclass
class Trade:
    session: object
    side: str
    entry_time: object
    entry_price: float
    stop: float
    target: float | None
    contracts: int
    r_dist: float
    exit_time: object = None
    exit_price: float = None
    reason: str = None
    pnl: float = 0.0
    r_multiple: float = 0.0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    daily: pd.DataFrame          # per-session pnl + equity
    config: ORBConfig
    n_days: int
    skipped_days: int


class ORBBacktester:
    def __init__(self, cfg: ORBConfig):
        self.cfg = cfg
        self.spec = cfg.instrument_spec()
        # resolve commission: explicit config wins, else instrument default
        self.commission = cfg.commission if cfg.commission is not None \
            else self.spec.commission

    # -- public ------------------------------------------------------------
    def run(self, df: pd.DataFrame) -> BacktestResult:
        cfg = self.cfg
        equity = cfg.starting_capital
        all_trades: list[Trade] = []
        daily_rows = []
        n_days = skipped = 0

        for day, g in sessions(df):
            n_days += 1
            g = self._prepare(g)
            or_high, or_low = self._opening_range(g)
            if or_high is None:
                skipped += 1
                continue
            day_trades, equity = self._run_session(day, g, or_high, or_low, equity)
            pnl = sum(t.pnl for t in day_trades)
            all_trades.extend(day_trades)
            daily_rows.append({"session": day, "trades": len(day_trades),
                               "pnl": pnl, "equity": equity})

        trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
        daily_df = pd.DataFrame(daily_rows)
        return BacktestResult(trades_df, daily_df, cfg, n_days, skipped)

    # -- per-session helpers ----------------------------------------------
    def _prepare(self, g: pd.DataFrame) -> pd.DataFrame:
        cfg = self.cfg
        g = g.copy()
        # session-anchored VWAP (§7.1)
        tp = (g.high + g.low + g.close) / 3.0
        cum_v = g.volume.cumsum().replace(0, np.nan)
        g["vwap"] = (tp * g.volume).cumsum() / cum_v
        g["vwap"] = g["vwap"].ffill().bfill()
        # ATR on exec bars (for atr stop / trailing)
        prev_c = g.close.shift(1)
        tr = pd.concat([g.high - g.low, (g.high - prev_c).abs(),
                        (g.low - prev_c).abs()], axis=1).max(axis=1)
        g["atr"] = tr.rolling(cfg.atr_len, min_periods=1).mean()
        return g

    def _opening_range(self, g: pd.DataFrame):
        cfg = self.cfg
        open_t = cfg.session_open
        end_min = open_t.hour * 60 + open_t.minute + cfg.or_minutes
        end_t = time(end_min // 60, end_min % 60)
        or_bars = g.between_time(open_t, end_t, inclusive="left")
        if len(or_bars) == 0:
            return None, None
        or_high = float(or_bars.high.max())
        or_low = float(or_bars.low.min())
        if (or_high - or_low) < cfg.min_range_ticks * self.spec.tick_size:
            return None, None
        return or_high, or_low

    def _run_session(self, day, g, or_high, or_low, equity):
        cfg, spec = self.cfg, self.spec
        tick = spec.tick_size
        buf = cfg.break_buffer_ticks * tick
        or_range = or_high - or_low

        # bars strictly after the opening range
        open_t = cfg.session_open
        end_min = open_t.hour * 60 + open_t.minute + cfg.or_minutes
        start_t = time(end_min // 60, end_min % 60)
        active = g.between_time(start_t, cfg.session_close)

        pos = None
        pending = None                 # queued market entry -> fill next-bar open
        armed_long = armed_short = True
        retest_long = retest_short = None   # {'expires': idx}
        n_side = {"long": 0, "short": 0}
        n_day = 0
        trades: list[Trade] = []

        rows = list(active.itertuples())
        for i, bar in enumerate(rows):
            t = bar.Index.time()

            # 0) EOD force-flat (§3) -----------------------------------------
            if pos is not None and t >= cfg.eod_flat:
                pos, tr = self._close(pos, bar.open, bar.Index, "eod", market=True)
                equity += tr.pnl; trades.append(tr)
                armed_long = armed_short = True
            if t >= cfg.eod_flat:
                pending = None
                continue

            # 1) fill queued market entry at this bar's open -----------------
            if pending is not None and pos is None:
                pos = self._open(pending["side"], bar.open, bar.Index,
                                  or_high, or_low, or_range, bar.atr, equity,
                                  market=True)
                pending = None
                if pos is not None:
                    n_side[pos.side] += 1; n_day += 1

            # 2) manage open position ---------------------------------------
            if pos is not None:
                pos.bars_in_trade += 1
                ex = self._intrabar_exit(pos, bar)
                if ex is None and self._time_stop_hit(pos, bar, or_high, or_low):
                    ex = (bar.close, "time_stop", True)
                if ex is None and cfg.exit_mode == "trail":
                    self._update_trail(pos, bar)
                    ex = self._intrabar_exit(pos, bar)  # re-check after trail
                if ex is not None:
                    price, reason, market = ex
                    pos, tr = self._close(pos, price, bar.Index, reason, market)
                    equity += tr.pnl; trades.append(tr)
                    # disarm the side just traded; it re-arms on re-entry inside
                    if tr.side == "long": armed_long = False
                    else: armed_short = False

            # 3) re-arm when price is back inside the range (§5.4) -----------
            if bar.close <= or_high: armed_long = True
            if bar.close >= or_low:  armed_short = True

            # 4) look for new entries (flat, before cutoff) -----------------
            if pos is None and pending is None and t < cfg.entry_cutoff \
                    and n_day < cfg.max_trades_day:
                if cfg.variant == "retest":
                    pending, retest_long, retest_short = self._retest_logic(
                        i, bar, or_high, or_low, buf, armed_long, armed_short,
                        retest_long, retest_short)
                    # consume arm when a breakout starts a retest watch
                    if retest_long is not None and bar.close > or_high + buf:
                        armed_long = False
                    if retest_short is not None and bar.close < or_low - buf:
                        armed_short = False
                    # touch-mode fills immediately (pending may be dict w/ open now)
                    if pending is not None and pending.get("immediate"):
                        pos = self._open(pending["side"], pending["price"],
                                         bar.Index, or_high, or_low, or_range,
                                         bar.atr, equity, market=False)
                        pending = None
                        if pos is not None:
                            n_side[pos.side] += 1; n_day += 1
                else:
                    pending = self._breakout_logic(
                        bar, or_high, or_low, buf, armed_long, armed_short,
                        n_side)
                    if pending is not None:
                        if pending["side"] == "long": armed_long = False
                        else: armed_short = False
                        if cfg.fill_on_close:  # aggressive: fill at this close
                            pos = self._open(pending["side"], bar.close, bar.Index,
                                             or_high, or_low, or_range, bar.atr,
                                             equity, market=True)
                            pending = None
                            if pos is not None:
                                n_side[pos.side] += 1; n_day += 1

        # safety: close anything still open at last bar
        if pos is not None and rows:
            last = rows[-1]
            pos, tr = self._close(pos, last.close, last.Index, "eod", market=True)
            equity += tr.pnl; trades.append(tr)
        return trades, equity

    # -- entry logic -------------------------------------------------------
    def _breakout_logic(self, bar, or_high, or_low, buf, armed_long, armed_short, n_side):
        cfg = self.cfg
        # VWAP gate (§7.2) applies to classic-with-vwap
        gate_long = gate_short = True
        if cfg.use_vwap:
            gate_long = bar.close > bar.vwap
            gate_short = bar.close < bar.vwap
        if (armed_long and gate_long and bar.close > or_high + buf
                and n_side["long"] < cfg.max_trades_per_side):
            return {"side": "long"}
        if (armed_short and gate_short and bar.close < or_low - buf
                and n_side["short"] < cfg.max_trades_per_side):
            return {"side": "short"}
        return None

    def _retest_logic(self, i, bar, or_high, or_low, buf, armed_long, armed_short,
                      retest_long, retest_short):
        cfg, tick = self.cfg, self.spec.tick_size
        rbuf = cfg.retest_buffer_ticks * tick
        pending = None

        # arm a retest watch on a fresh breakout close
        if retest_long is None and armed_long and bar.close > or_high + buf:
            retest_long = {"expires": i + cfg.retest_window}
        if retest_short is None and armed_short and bar.close < or_low - buf:
            retest_short = {"expires": i + cfg.retest_window}

        # long retest
        if retest_long is not None:
            if bar.close < or_low or i > retest_long["expires"]:
                retest_long = None                      # failed / expired
            elif bar.low <= or_high + rbuf:             # pullback tag
                if cfg.retest_confirm == "touch":
                    pending = {"side": "long", "immediate": True, "price": or_high}
                    retest_long = None
                elif bar.close > or_high:               # reject: close back above
                    pending = {"side": "long"}
                    retest_long = None
        # short retest
        if pending is None and retest_short is not None:
            if bar.close > or_high or i > retest_short["expires"]:
                retest_short = None
            elif bar.high >= or_low - rbuf:
                if cfg.retest_confirm == "touch":
                    pending = {"side": "short", "immediate": True, "price": or_low}
                    retest_short = None
                elif bar.close < or_low:
                    pending = {"side": "short"}
                    retest_short = None
        return pending, retest_long, retest_short

    # -- position open/close ----------------------------------------------
    def _open(self, side, price, ts, or_high, or_low, or_range, atr, equity, market):
        cfg, spec = self.cfg, self.spec
        slip = cfg.slippage_ticks * spec.tick_size if market else 0.0
        entry = price + slip if side == "long" else price - slip
        stop = self._stop_price(side, entry, or_high, or_low, atr)
        r_dist = abs(entry - stop)
        if r_dist <= 0:
            return None
        target = self._target_price(side, entry, r_dist, or_range)
        contracts = self._size(equity, r_dist)
        if contracts <= 0:
            return None
        p = Trade(session=ts.date(), side=side, entry_time=ts, entry_price=entry,
                  stop=stop, target=target, contracts=contracts, r_dist=r_dist)
        p.bars_in_trade = 0
        p.extreme = entry
        return p

    def _close(self, pos, price, ts, reason, market):
        cfg, spec = self.cfg, self.spec
        slip = cfg.slippage_ticks * spec.tick_size if market else 0.0
        exit_px = price - slip if pos.side == "long" else price + slip
        sign = 1 if pos.side == "long" else -1
        gross = sign * (exit_px - pos.entry_price) * spec.point_value * pos.contracts
        commission = 2 * self.commission * pos.contracts
        pos.exit_time = ts
        pos.exit_price = exit_px
        pos.reason = reason
        pos.pnl = gross - commission
        pos.r_multiple = sign * (exit_px - pos.entry_price) / pos.r_dist
        return None, pos

    # -- exit checks -------------------------------------------------------
    def _intrabar_exit(self, pos, bar):
        slip = self.cfg.slippage_ticks * self.spec.tick_size
        if pos.side == "long":
            if bar.open <= pos.stop:
                return (bar.open, "stop", True)
            if bar.low <= pos.stop:                     # stop-first priority
                return (pos.stop, "stop", True)
            if pos.target is not None and bar.high >= pos.target:
                return (pos.target, "target", False)
        else:
            if bar.open >= pos.stop:
                return (bar.open, "stop", True)
            if bar.high >= pos.stop:
                return (pos.stop, "stop", True)
            if pos.target is not None and bar.low <= pos.target:
                return (pos.target, "target", False)
        return None

    def _time_stop_hit(self, pos, bar, or_high, or_low):
        cfg = self.cfg
        if not cfg.use_time_stop or pos.bars_in_trade > cfg.time_stop_bars:
            return False
        # trade re-enters the range and isn't yet 0.5R in profit
        sign = 1 if pos.side == "long" else -1
        unreal_r = sign * (bar.close - pos.entry_price) / pos.r_dist
        if unreal_r >= 0.5:
            return False
        inside = bar.close < or_high if pos.side == "long" else bar.close > or_low
        return inside

    def _update_trail(self, pos, bar):
        cfg = self.cfg
        atr = bar.atr if not np.isnan(bar.atr) else pos.r_dist
        if pos.side == "long":
            pos.extreme = max(pos.extreme, bar.high)
            if (pos.extreme - pos.entry_price) >= cfg.trail_arm_R * pos.r_dist:
                pos.stop = max(pos.stop, pos.extreme - cfg.trail_atr_mult * atr)
        else:
            pos.extreme = min(pos.extreme, bar.low)
            if (pos.entry_price - pos.extreme) >= cfg.trail_arm_R * pos.r_dist:
                pos.stop = min(pos.stop, pos.extreme + cfg.trail_atr_mult * atr)

    # -- pricing helpers ---------------------------------------------------
    def _stop_price(self, side, entry, or_high, or_low, atr):
        cfg, tick = self.cfg, self.spec.tick_size
        mode = cfg.stop_mode
        if mode == "opposite_range":
            return or_low if side == "long" else or_high
        if mode == "range_mid":
            mid = (or_high + or_low) / 2
            return mid
        if mode == "atr":
            a = atr if not np.isnan(atr) else (or_high - or_low)
            return entry - cfg.atr_mult * a if side == "long" else entry + cfg.atr_mult * a
        if mode == "fixed_ticks":
            d = cfg.stop_ticks * tick
            return entry - d if side == "long" else entry + d
        return or_low if side == "long" else or_high

    def _target_price(self, side, entry, r_dist, or_range):
        cfg = self.cfg
        if cfg.exit_mode == "trail":
            return None
        if cfg.exit_mode == "measured_move":
            return entry + or_range if side == "long" else entry - or_range
        # r_multiple
        return entry + cfg.target_R * r_dist if side == "long" \
            else entry - cfg.target_R * r_dist

    def _size(self, equity, r_dist):
        cfg, spec = self.cfg, self.spec
        if cfg.sizing == "fixed_contracts":
            return min(cfg.fixed_contracts, cfg.max_contracts)
        risk = equity * cfg.risk_pct
        raw = risk / (r_dist * spec.point_value)
        return int(min(max(np.floor(raw), 0), cfg.max_contracts))
