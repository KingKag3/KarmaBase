"""Configuration for the ORB engine.

Maps directly to strategies/orb.md. One `ORBConfig` describes the shared engine;
the three baseline variants (Classic / VWAP / Breakout-Retest) are factory
presets. Instrument specs cover the real futures (ES/NQ) and the prototyping
proxies (SPY/QQQ).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time


# ---------------------------------------------------------------------------
# Instrument specifications
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: float      # minimum price increment
    point_value: float    # $ per 1.0 of price movement per contract/share
    yf_symbol: str        # ticker used by the yfinance loader (proxy for futures)
    commission: float     # $ per side per unit (futures: per contract; equity: per share)


INSTRUMENTS: dict[str, Instrument] = {
    # Real futures (require paid intraday data for final validation)
    "ES": Instrument("ES", tick_size=0.25, point_value=50.0, yf_symbol="SPY", commission=2.50),
    "NQ": Instrument("NQ", tick_size=0.25, point_value=20.0, yf_symbol="QQQ", commission=2.50),
    # Free proxies for prototyping the engine mechanics (retail equity ~ commission-free;
    # cost is carried by slippage_ticks). Per-share commission ~ $0.
    "SPY": Instrument("SPY", tick_size=0.01, point_value=1.0, yf_symbol="SPY", commission=0.0),
    "QQQ": Instrument("QQQ", tick_size=0.01, point_value=1.0, yf_symbol="QQQ", commission=0.0),
}


# ---------------------------------------------------------------------------
# ORB configuration
# ---------------------------------------------------------------------------
@dataclass
class ORBConfig:
    # --- identity ---
    variant: str = "classic"            # classic | vwap | retest
    instrument: str = "SPY"

    # --- session / range (§3, §5.1) ---
    or_minutes: int = 15
    exec_tf: str = "5m"                 # bar size: "1m" or "5m"
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    entry_cutoff: time = time(15, 30)   # no new trades after this
    eod_flat: time = time(15, 55)       # force-flat time
    min_range_ticks: int = 4            # skip degenerate ranges

    # --- entry (§5.2, §5.3) ---
    break_buffer_ticks: int = 0
    fill_on_close: bool = False         # False => fill at next-bar open (no look-ahead)

    # --- re-arm / frequency (§5.4) ---
    max_trades_per_side: int = 10_000   # effectively unlimited
    max_trades_day: int = 10_000

    # --- stops (§5.5) ---
    stop_mode: str = "opposite_range"   # opposite_range | range_mid | atr | fixed_ticks
    atr_len: int = 14
    atr_mult: float = 1.0
    stop_ticks: int = 40

    # --- targets / exits (§5.6) ---
    exit_mode: str = "r_multiple"       # r_multiple | measured_move | trail
    target_R: float = 1.0
    use_time_stop: bool = True
    time_stop_bars: int = 6
    trail_arm_R: float = 1.0
    trail_atr_mult: float = 1.5

    # --- costs (§5.7) ---
    commission: float | None = None     # $/side/unit; None => use instrument default
    slippage_ticks: int = 1             # per side

    # --- sizing (§5.8) ---
    sizing: str = "risk_pct"            # risk_pct | fixed_contracts
    risk_pct: float = 0.01
    fixed_contracts: int = 1
    max_contracts: int = 10
    starting_capital: float = 100_000.0

    # --- variant B: VWAP gate (§7) ---
    use_vwap: bool = False
    use_vwap_slope: bool = False
    vwap_slope_lookback: int = 3

    # --- variant C: breakout-retest (§8) ---
    retest_window: int = 8
    retest_buffer_ticks: int = 2
    retest_confirm: str = "touch"       # touch | reject

    def instrument_spec(self) -> Instrument:
        return INSTRUMENTS[self.instrument]

    @property
    def bars_per_or(self) -> int:
        step = 1 if self.exec_tf == "1m" else 5
        return max(1, self.or_minutes // step)


# ---------------------------------------------------------------------------
# Variant presets (the three baselines from orb.md)
# ---------------------------------------------------------------------------
def classic(instrument: str = "SPY", **overrides) -> ORBConfig:
    return replace(ORBConfig(variant="classic", instrument=instrument), **overrides)


def vwap(instrument: str = "SPY", **overrides) -> ORBConfig:
    base = ORBConfig(variant="vwap", instrument=instrument, use_vwap=True)
    return replace(base, **overrides)


def retest(instrument: str = "SPY", **overrides) -> ORBConfig:
    base = ORBConfig(variant="retest", instrument=instrument)
    return replace(base, **overrides)


PRESETS = {"classic": classic, "vwap": vwap, "retest": retest}
