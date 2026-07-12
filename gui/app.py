"""KarmaBase — strategy research dashboard (Streamlit).

Local GUI that discovers strategy specs (strategies/*.md manifests), runs the
karmabase backtester with UI-chosen params, shows metrics + charts (single or
variant-comparison, in-sample vs out-of-sample, vs buy & hold), and exports a
TradingView Pine v6 strategy.

Run:  streamlit run gui/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karmabase import registry as reg
from karmabase.config import INSTRUMENTS
from karmabase.data import load_dukascopy, load_intraday
from karmabase.engine import ORBBacktester
from karmabase.metrics import compute
from karmabase.pine import to_pine

st.set_page_config(page_title="KarmaBase", page_icon="📈", layout="wide")
OOS_SPLIT = 2022                       # first out-of-sample year (Dukascopy data)


# ============================ helpers =====================================
@st.cache_data(show_spinner=False)
def load_data(instrument: str, interval: str, y0: int, y1: int) -> pd.DataFrame:
    spec = INSTRUMENTS[instrument]
    if spec.duka:                      # ES/NQ -> Dukascopy index CFD (near-24h)
        return load_dukascopy(spec.duka, interval, y0, y1)
    return load_intraday(spec.yf_symbol, interval=interval)   # SPY/QQQ proxy


def slice_years(df, y0, y1):
    return df[(df.index.year >= y0) & (df.index.year <= y1)]


def run_bt(man, variant, instrument, overrides, df):
    res = ORBBacktester(reg.build_config(man, variant, instrument, overrides)).run(df)
    return res, compute(res)


def years_span(df):
    return max((df.index.max() - df.index.min()).days / 365.25, 0.1)


def cagr(m, yrs):
    if not m.get("n_trades") or yrs <= 0:
        return 0.0
    g = m["final_equity"] / (m["final_equity"] - m["total_pnl"])
    return (g ** (1 / yrs) - 1) * 100 if g > 0 else 0.0


def equity_dd(res, cap):
    """Return (equity Series, drawdown% Series) indexed by session date."""
    d = res.daily.copy()
    d["session"] = pd.to_datetime(d["session"])
    eq = d.set_index("session")["equity"]
    dd = (eq / eq.cummax() - 1.0) * 100
    return eq, dd


def benchmark(df, cap):
    """Buy & hold of the underlying, normalized to starting capital."""
    daily_close = df["close"].groupby(df.index.date).last()
    daily_close.index = pd.to_datetime(daily_close.index)
    return cap * daily_close / daily_close.iloc[0]


def verdict(m):
    pf, sh = m.get("profit_factor", 0), m.get("sharpe", 0)
    if pf >= 1.3 and sh >= 1.0:
        return "success", "✅ Promising — worth deeper validation (walk-forward, real fills)."
    if pf >= 1.0 and sh > 0:
        return "warning", "🟡 Marginal — weak/likely not tradeable after realistic costs."
    return "error", "🔴 No edge — loses money on this window."


def metrics_row(m, yrs, bench_ret=None):
    c = st.columns(6)
    c[0].metric("Net return", f"{m['total_return_pct']:.1f}%",
                delta=None if bench_ret is None else f"{m['total_return_pct']-bench_ret:+.1f}% vs B&H")
    c[1].metric("CAGR", f"{cagr(m, yrs):.1f}%")
    c[2].metric("Sharpe", f"{m.get('sharpe',0):.2f}")
    c[3].metric("Profit factor", f"{m['profit_factor']:.2f}")
    c[4].metric("Max DD", f"{m.get('max_drawdown_pct',0):.1f}%")
    c[5].metric("Trades", f"{m['n_trades']}  ·  {m['win_rate']*100:.0f}% win")


def is_oos_table(man, variant, instrument, overrides, df):
    rows = []
    for label, sub in [("In-sample <" + str(OOS_SPLIT), slice_years(df, 1900, OOS_SPLIT - 1)),
                       ("Out-of-sample ≥" + str(OOS_SPLIT), slice_years(df, OOS_SPLIT, 2100))]:
        if len(sub) == 0:
            continue
        _, m = run_bt(man, variant, instrument, overrides, sub)
        if m.get("n_trades"):
            rows.append({"split": label, "trades": m["n_trades"],
                         "CAGR%": round(cagr(m, years_span(sub)), 1),
                         "Sharpe": round(m.get("sharpe", 0), 2),
                         "PF": round(m["profit_factor"], 2),
                         "maxDD%": round(m.get("max_drawdown_pct", 0), 1),
                         "win%": round(m["win_rate"] * 100, 1)})
    return pd.DataFrame(rows)


def per_year(man, variant, instrument, overrides, df):
    rows = []
    for y in range(df.index.year.min(), df.index.year.max() + 1):
        sub = slice_years(df, y, y)
        if len(sub) == 0:
            continue
        _, m = run_bt(man, variant, instrument, overrides, sub)
        if m.get("n_trades"):
            rows.append({"year": y, "trades": m["n_trades"],
                         "win%": round(m["win_rate"] * 100, 1),
                         "avgR": round(m["avg_R"], 3), "PF": round(m["profit_factor"], 2),
                         "Sharpe": round(m.get("sharpe", 0), 2),
                         "maxDD%": round(m.get("max_drawdown_pct", 0), 1),
                         "ret%": round(m["total_return_pct"], 1)})
    return pd.DataFrame(rows)


def widget_for(pname, schema):
    label, default = schema.get("label", pname), schema.get("default")
    if "choices" in schema:
        opts = schema["choices"]
        return st.selectbox(label, opts, index=opts.index(default) if default in opts else 0)
    if isinstance(default, bool):
        return st.checkbox(label, value=default)
    if "min" in schema and "max" in schema:
        return st.slider(label, float(schema["min"]), float(schema["max"]),
                         float(default), float(schema.get("step", 0.1)))
    return st.text_input(label, str(default))


# ============================ sidebar =====================================
st.title("📈 KarmaBase — Strategy Research Dashboard")

manifests = reg.discover()
if not manifests:
    st.error("No strategy manifests found in strategies/*.md")
    st.stop()

with st.sidebar:
    st.header("Strategy")
    by_name = {m.name: m for m in manifests}
    man = by_name[st.selectbox("Strategy", list(by_name))]
    if man.status:
        st.caption(f"status: `{man.status}`")
    compare = st.toggle("Compare all variants", value=False)
    variant = st.selectbox("Variant", man.variants, disabled=compare)
    instrument = st.selectbox("Instrument", man.instruments)
    spec = INSTRUMENTS[instrument]
    st.caption(f"data: {'Dukascopy CFD (near-24h)' if spec.duka else 'yfinance proxy (~60d)'}")

    st.header("Parameters")
    overrides = {p: widget_for(p, s) for p, s in man.params.items()}
    interval = overrides.get("exec_tf", "5m")

    st.header("Backtest window")
    if spec.duka:
        y0, y1 = st.select_slider("Years", options=list(range(2015, 2026)), value=(2018, 2025))
    else:
        y0, y1 = 2000, 2100
        st.caption("Proxy data is a fixed recent window.")
    run = st.button("▶ Run backtest", type="primary", use_container_width=True)

st.write(f"**{man.name}** — {man.description}")
tab_bt, tab_pine, tab_spec = st.tabs(["📊 Backtest", "📤 TradingView export", "📄 Spec"])

# ============================ backtest tab ================================
with tab_bt:
    if not run:
        st.info("Set parameters in the sidebar and press **Run backtest**.")
    else:
        with st.spinner("Loading data & running backtest..."):
            df = load_data(instrument, interval, y0, y1)
            df = slice_years(df, y0, y1) if spec.duka else df
        yrs = years_span(df)
        st.caption(f"window: {df.index.min().date()} → {df.index.max().date()}  ·  {yrs:.1f}y")

        if compare:
            # -------- variant comparison --------
            eq_all, rows = {}, []
            for v in man.variants:
                res, m = run_bt(man, v, instrument, overrides, df)
                if not m.get("n_trades"):
                    continue
                eq, _ = equity_dd(res, 100_000)
                eq_all[v] = eq
                rows.append({"variant": v, "CAGR%": round(cagr(m, yrs), 1),
                             "Sharpe": round(m.get("sharpe", 0), 2),
                             "PF": round(m["profit_factor"], 2),
                             "maxDD%": round(m.get("max_drawdown_pct", 0), 1),
                             "win%": round(m["win_rate"] * 100, 1),
                             "trades": m["n_trades"], "verdict": verdict(m)[1][:2]})
            st.subheader("Variant comparison")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.subheader("Equity curves")
            bench = benchmark(df, 100_000)
            combo = pd.DataFrame(eq_all)
            combo["buy & hold"] = bench.reindex(combo.index, method="ffill")
            st.line_chart(combo)
        else:
            # -------- single variant --------
            res, m = run_bt(man, variant, instrument, overrides, df)
            if not m.get("n_trades"):
                st.warning("No trades produced for this configuration.")
                st.stop()
            lvl, msg = verdict(m)
            getattr(st, lvl)(msg)
            bench = benchmark(df, 100_000)
            metrics_row(m, yrs, bench_ret=(bench.iloc[-1] / bench.iloc[0] - 1) * 100)

            eq, dd = equity_dd(res, 100_000)
            st.subheader("Equity curve vs buy & hold")
            chart = pd.DataFrame({"strategy": eq,
                                  "buy & hold": bench.reindex(eq.index, method="ffill")})
            st.line_chart(chart)
            st.subheader("Drawdown (%)")
            st.area_chart(dd, color="#d62728")

            if spec.duka and y0 < OOS_SPLIT <= y1:
                st.subheader("In-sample vs out-of-sample")
                st.dataframe(is_oos_table(man, variant, instrument, overrides, df),
                             hide_index=True, use_container_width=True)

            left, right = st.columns(2)
            with left:
                st.subheader("Per-year")
                st.dataframe(per_year(man, variant, instrument, overrides, df),
                             hide_index=True, use_container_width=True)
            with right:
                st.subheader("Exit mix")
                st.bar_chart(pd.Series(m["exit_mix"]))

            st.subheader("Recent trades")
            cols = ["session", "side", "entry_time", "entry_price", "stop", "target",
                    "contracts", "exit_price", "reason", "pnl", "r_multiple"]
            st.dataframe(res.trades[cols].tail(30), hide_index=True, use_container_width=True)

# ============================ pine tab ====================================
with tab_pine:
    st.caption("Live-testing companion. TV fills/sessions differ from the karmabase "
               "engine — results will not match the backtest exactly.")
    v = man.variants[0] if compare else variant
    cfg = reg.build_config(man, v, instrument, overrides)
    pine = to_pine(cfg)
    st.download_button("⬇ Download .pine", pine,
                       file_name=f"karmabase_{man.id}_{v}_{instrument}.pine")
    st.code(pine, language="javascript")

# ============================ spec tab ====================================
with tab_spec:
    st.markdown(man.path.read_text(encoding="utf-8"))
