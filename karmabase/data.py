"""Data loading.

Prototype loader uses yfinance for SPY/QQQ intraday. NOTE (per orb.md §4/§12):
yfinance only serves intraday history for a short window (5m ~ last 60 days,
1m ~ last 7 days). This is enough to validate engine MECHANICS and get
preliminary numbers, NOT enough for the 8-year validation, which requires a
real ES/NQ intraday source. Keep that caveat attached to any proxy result.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

ET = "America/New_York"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

_MAX_PERIOD = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d"}


def load_intraday(
    yf_symbol: str,
    interval: str = "5m",
    period: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Return tz-aware (America/New_York) OHLCV bars for one symbol.

    Columns: open, high, low, close, volume. Index: DatetimeIndex in ET.
    """
    period = period or _MAX_PERIOD.get(interval, "60d")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{yf_symbol}_{interval}_{period}.csv"

    if use_cache and cache.exists():
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
        return _clean(df)

    import yfinance as yf

    raw = yf.download(
        yf_symbol,
        interval=interval,
        period=period,
        auto_adjust=False,
        prepost=False,
        progress=False,
    )
    if raw.empty:
        raise RuntimeError(
            f"yfinance returned no data for {yf_symbol} {interval}/{period}. "
            "Intraday history is limited (5m~60d, 1m~7d)."
        )
    # Flatten possible MultiIndex columns (single ticker still may come nested).
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]

    idx = pd.to_datetime(raw.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    raw.index = idx.tz_convert(ET)

    raw.to_csv(cache)
    return _clean(raw)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def sessions(df: pd.DataFrame):
    """Yield (session_date, day_df) for each trading day, RTH only."""
    rth = df.between_time("09:30", "16:00")
    for day, g in rth.groupby(rth.index.date):
        if len(g) >= 2:
            yield day, g


# ---------------------------------------------------------------------------
# Dukascopy index-CFD loader (near-24h proxy for ES/NQ futures)
# ---------------------------------------------------------------------------
_DUKA_INTERVAL = {"1m": "INTERVAL_MIN_1", "5m": "INTERVAL_MIN_5",
                  "15m": "INTERVAL_MIN_15", "1h": "INTERVAL_HOUR_1"}


def load_dukascopy(duka_code: str, interval: str = "5m",
                   start_year: int = 2015, end_year: int | None = None,
                   use_cache: bool = True) -> pd.DataFrame:
    """Fetch Dukascopy index-CFD OHLCV, cached per calendar year, returned in ET.

    Data source is nearly 24h and tracks the cash index, so the 09:30 ET open and
    gap behavior resemble the futures far better than RTH-only SPY/QQQ. It is a
    CFD (no contract roll); treat as a high-fidelity proxy, not the exact contract.
    """
    import datetime as dt
    import logging

    import dukascopy_python as dk
    from dukascopy_python import instruments as _ins

    logging.getLogger("DUKASCRIPT").setLevel(logging.WARNING)  # silence per-month spam
    end_year = end_year or dt.date.today().year
    interval_const = getattr(dk, _DUKA_INTERVAL[interval])
    # resolve the instrument constant whose value matches duka_code
    inst = next(getattr(_ins, n) for n in dir(_ins)
                if getattr(_ins, n) == duka_code)
    safe = duka_code.replace("/", "_").replace("&", "and")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    frames = []
    today = dt.date.today()
    for year in range(start_year, end_year + 1):
        cache = CACHE_DIR / f"duka_{safe}_{interval}_{year}.csv"
        if use_cache and cache.exists():
            part = pd.read_csv(cache, index_col=0, parse_dates=True)
            part.index = pd.to_datetime(part.index, utc=True)
        else:
            end = min(dt.datetime(year + 1, 1, 1), dt.datetime(today.year, today.month, today.day))
            if dt.datetime(year, 1, 1) >= end:
                continue
            part = dk.fetch(inst, interval_const, dk.OFFER_SIDE_BID,
                            dt.datetime(year, 1, 1), end)
            if part.empty:
                continue
            part.index = pd.to_datetime(part.index, utc=True)
            part = part[["open", "high", "low", "close", "volume"]].astype(float)
            # only cache complete past years
            if year < today.year:
                part.to_csv(cache)
        frames.append(part)

    if not frames:
        raise RuntimeError(f"No Dukascopy data for {duka_code} {start_year}-{end_year}.")
    df = pd.concat(frames)
    df.index = df.index.tz_convert(ET)
    return _clean(df)
