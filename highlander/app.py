#!/usr/bin/env python3
"""
Hypothesis Highlander — live archive-grid + Pareto visual (the "it's learning" picture).
Run:  .venv/bin/python -m streamlit run highlander/app.py   (from repo root)
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import plotly.graph_objects as go
import streamlit as st

from highlander.controller import Highlander
from highlander import viz
from highlander.genome import AXES

st.set_page_config(page_title="Hypothesis Highlander", page_icon="⚔️", layout="wide")


def md(t):  # escape $ so Streamlit markdown doesn't treat it as LaTeX
    return str(t).replace("$", "\\$")


st.sidebar.title("⚔️ Highlander search")
generations = st.sidebar.slider("Generations", 1, 8, 4)
pop_size = st.sidebar.slider("Population / generation", 6, 40, 18)
budget = st.sidebar.select_slider("Eval budget (units)", [800, 1600, 2600, 4000, 8000], value=2600)
seed = st.sidebar.number_input("RNG seed", 0, 9999, 42)
use_llm = st.sidebar.checkbox("Claude generator (live)", value=False,
                              help="Off = deterministic offline generator (no API). On = Claude proposes hypotheses.")
run = st.sidebar.button("▶ Run search", type="primary")

st.title("Hypothesis Highlander")
st.caption("Quality-diversity evolutionary search over RA drug-program hypotheses — MAP-Elites "
           "illumination + Pareto trade-off front + a cost-gated fitness cascade (the legacy ROI tier is a NOT_DECISION_GRADE proxy).")

if "res" not in st.session_state or run:
    with st.spinner("Evolving hypotheses…"):
        hl = Highlander(seed=int(seed), budget_units=int(budget), use_llm=use_llm)
        st.session_state.res = hl.run(generations=int(generations), pop_size=int(pop_size))
        st.session_state.hl = hl
res = st.session_state.res
hl = st.session_state.hl

# ── headline metrics ──
last = res["history"][-1]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Niches illuminated", last["cells_filled"])
c2.metric("Pareto front", last["pareto_front"])
c3.metric("Hypotheses evaluated", sum(h["evaluated"] for h in res["history"]))
c4.metric("Deaths → failure ledger", len(res["failure_ledger"]))

colL, colR = st.columns(2)

# ── learning curve (the "it's learning" signal) ──
with colL:
    st.subheader("Is it learning? — coverage & Pareto per generation")
    lc = viz.learning_curve(res["history"])
    fig = go.Figure()
    fig.add_scatter(x=lc["generation"], y=lc["coverage"], mode="lines+markers", name="niches illuminated",
                    line_color="#4C78A8")
    fig.add_scatter(x=lc["generation"], y=lc["pareto"], mode="lines+markers", name="Pareto front size",
                    line_color="#E45756")
    fig.update_layout(xaxis_title="generation", yaxis_title="count", height=340,
                      margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Coverage rising = the search is exploring new biological niches; a growing/advancing "
               "Pareto front = it's finding better non-dominated programs each generation.")

# ── MAP-Elites illumination grid ──
with colR:
    st.subheader("MAP-Elites grid — champion per biomarker × modality")
    gm = viz.grid_matrix(hl.archive, "biomarker", "modality")
    z = [[np.nan if v is None else v for v in row] for row in gm["z"]]
    fig = go.Figure(go.Heatmap(z=z, x=gm["cols"], y=gm["rows"], colorscale="Viridis",
                               text=gm["label"], hovertemplate="%{y} / %{x}<br>composite %{z}<br>%{text}<extra></extra>",
                               colorbar=dict(title="composite")))
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Each filled cell is the best hypothesis in that biological niche (blanks = unexplored). "
               "Boldness (supported→crazy) is the third niche axis — kept so 'crazy' ideas aren't averaged away.")

# ── Pareto front on two chosen axes ──
st.subheader("Pareto trade-off front (the BD deliverable — not one winner)")
pcol1, pcol2, _ = st.columns([1, 1, 3])
x_axis = pcol1.selectbox("X axis", AXES, index=AXES.index("recruitability"))
y_axis = pcol2.selectbox("Y axis", AXES, index=AXES.index("bio_reality"))
p = viz.pareto_2d(hl.archive, x_axis, y_axis)
fig = go.Figure()
if p["dominated"]:
    fig.add_scatter(x=[d["x"] for d in p["dominated"]], y=[d["y"] for d in p["dominated"]], mode="markers",
                    name="dominated", marker=dict(color="#B0B0B0", size=8),
                    text=[d["label"] for d in p["dominated"]], hovertemplate="%{text}<extra></extra>")
front = sorted(p["pareto"], key=lambda d: d["x"])
fig.add_scatter(x=[d["x"] for d in front], y=[d["y"] for d in front], mode="lines+markers+text",
                name="Pareto front", marker=dict(color="#E45756", size=12), line_color="#E45756",
                text=[d["label"].split("/")[0] for d in front], textposition="top center",
                hovertext=[d["label"] for d in front], hovertemplate="%{hovertext}<extra></extra>")
fig.update_layout(xaxis_title=x_axis, yaxis_title=y_axis, height=420,
                  margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
st.plotly_chart(fig, use_container_width=True)

# ── axis leaders + top programs + failures ──
a1, a2 = st.columns(2)
with a1:
    st.subheader("Best on each axis")
    for axis, d in viz.axis_leaders(hl.archive).items():
        st.markdown(md(f"- **{axis}** → {d['label']}  ·  {d['score']:.2f}"))
with a2:
    st.subheader("Failure ledger (feeds the next generation)")
    from collections import Counter
    by = Counter(f["dropped_at"] for f in res["failure_ledger"])
    st.markdown(md("Drops by gate: " + (", ".join(f"**{k}** {v}" for k, v in by.items()) or "none")))
    st.caption("The generator reads these to avoid known dead-ends — cheap 'learning from each run'.")

st.subheader("Top programs (composite)")
st.dataframe([{"biomarker": g["biomarker"], "modality": g["modality"], "boldness": g["boldness"],
               "plaus": g["scores"].get("plausibility"), "roi": g["scores"].get("roi"),
               "recruit": g["scores"].get("recruitability"), "bio": g["scores"].get("bio_reality")}
              for g in res["top"]], use_container_width=True, hide_index=True)
st.caption(f"Reproducible from seed={res['seed']}. Legacy ROI tier = NOT_DECISION_GRADE proxy; T1/T4 are labeled "
           f"stubs behind a Tier interface (see highlander/README.md).")
