"""KQUANT — strategy research dashboard (Streamlit).

Local GUI that discovers strategy specs (strategies/*.md manifests), runs the
kquant backtester with UI-chosen params, shows metrics + charts, and exports a
TradingView Pine v6 strategy.

Run:  streamlit run gui/app.py     (or: python -m streamlit run gui/app.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kquant import registry as reg
from kquant.config import INSTRUMENTS
from kquant.data import load_dukascopy, load_intraday
from kquant.engine import ORBBacktester
from kquant.metrics import compute
from kquant.pine import to_pine

st.set_page_config(page_title="KQUANT", page_icon="📈", layout="wide")


# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_data(instrument: str, interval: str, y0: int, y1: int) -> pd.DataFrame:
    spec = INSTRUMENTS[instrument]
    if spec.duka:                      # ES/NQ -> Dukascopy index CFD (near-24h)
        return load_dukascopy(spec.duka, interval, y0, y1)
    return load_intraday(spec.yf_symbol, interval=interval)   # SPY/QQQ proxy


def slice_years(df, y0, y1):
    return df[(df.index.year >= y0) & (df.index.year <= y1)]


def per_year(cfg_builder, df, y0, y1):
    rows = []
    for y in range(y0, y1 + 1):
        sub = slice_years(df, y, y)
        if len(sub) == 0:
            continue
        m = compute(ORBBacktester(cfg_builder()).run(sub))
        if m.get("n_trades"):
            rows.append({"year": y, "trades": m["n_trades"],
                         "win%": round(m["win_rate"] * 100, 1),
                         "avgR": round(m["avg_R"], 3), "PF": round(m["profit_factor"], 2),
                         "Sharpe": round(m.get("sharpe", 0), 2),
                         "maxDD%": round(m.get("max_drawdown_pct", 0), 1),
                         "ret%": round(m["total_return_pct"], 1)})
    return pd.DataFrame(rows)


def widget_for(pname, schema):
    label = schema.get("label", pname)
    default = schema.get("default")
    if "choices" in schema:
        opts = schema["choices"]
        return st.selectbox(label, opts, index=opts.index(default) if default in opts else 0)
    if isinstance(default, bool):
        return st.checkbox(label, value=default)
    if "min" in schema and "max" in schema:
        return st.slider(label, float(schema["min"]), float(schema["max"]),
                         float(default), float(schema.get("step", 0.1)))
    return st.text_input(label, str(default))


# --------------------------------------------------------------------------
st.title("📈 KQUANT — Strategy Research Dashboard")

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
    variant = st.selectbox("Variant", man.variants)
    instrument = st.selectbox("Instrument", man.instruments)
    spec = INSTRUMENTS[instrument]
    src = "Dukascopy CFD (near-24h)" if spec.duka else "yfinance proxy (~60d)"
    st.caption(f"data: {src}")

    st.header("Parameters")
    overrides = {p: widget_for(p, s) for p, s in man.params.items()}
    interval = overrides.get("exec_tf", "5m")

    st.header("Backtest window")
    if spec.duka:
        y0, y1 = st.select_slider("Years", options=list(range(2015, 2026)),
                                  value=(2018, 2025))
    else:
        y0, y1 = 2000, 2100          # proxy: whatever yfinance returns
        st.caption("Proxy data is a fixed recent window.")
    run = st.button("▶ Run backtest", type="primary", use_container_width=True)

st.write(f"**{man.name}** — {man.description}")

tab_bt, tab_pine, tab_spec = st.tabs(["📊 Backtest", "📤 TradingView export", "📄 Spec"])

# ---- backtest tab --------------------------------------------------------
with tab_bt:
    if not run:
        st.info("Set parameters in the sidebar and press **Run backtest**.")
    else:
        with st.spinner("Loading data & running backtest..."):
            df = load_data(instrument, interval, y0, y1)
            df = slice_years(df, y0, y1) if spec.duka else df
            builder = lambda: reg.build_config(man, variant, instrument, overrides)
            res = ORBBacktester(builder()).run(df)
            m = compute(res)

        if not m.get("n_trades"):
            st.warning("No trades produced for this configuration.")
        else:
            c = st.columns(6)
            c[0].metric("Net return", f"{m['total_return_pct']:.1f}%")
            c[1].metric("Sharpe", f"{m.get('sharpe',0):.2f}")
            c[2].metric("Profit factor", f"{m['profit_factor']:.2f}")
            c[3].metric("Max DD", f"{m.get('max_drawdown_pct',0):.1f}%")
            c[4].metric("Win rate", f"{m['win_rate']*100:.1f}%")
            c[5].metric("Trades", f"{m['n_trades']}")

            eq = res.daily.copy()
            eq["session"] = pd.to_datetime(eq["session"])
            st.subheader("Equity curve")
            st.line_chart(eq.set_index("session")["equity"])

            left, right = st.columns([1, 1])
            with left:
                st.subheader("Per-year")
                st.dataframe(per_year(builder, df, df.index.year.min(),
                                      df.index.year.max()), hide_index=True,
                             use_container_width=True)
            with right:
                st.subheader("Exit mix")
                st.bar_chart(pd.Series(m["exit_mix"]))

            st.subheader("Recent trades")
            cols = ["session", "side", "entry_time", "entry_price", "stop",
                    "target", "contracts", "exit_price", "reason", "pnl", "r_multiple"]
            st.dataframe(res.trades[cols].tail(30), hide_index=True,
                         use_container_width=True)

# ---- pine tab ------------------------------------------------------------
with tab_pine:
    st.caption("Live-testing companion. TV fills/sessions differ from the kquant "
               "engine — results will not match the backtest exactly.")
    cfg = reg.build_config(man, variant, instrument, overrides)
    pine = to_pine(cfg)
    st.download_button("⬇ Download .pine", pine,
                       file_name=f"kquant_{man.id}_{variant}_{instrument}.pine")
    st.code(pine, language="javascript")

# ---- spec tab ------------------------------------------------------------
with tab_spec:
    st.markdown(man.path.read_text(encoding="utf-8"))
