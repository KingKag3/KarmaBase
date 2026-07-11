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
