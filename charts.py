"""
charts.py

Plotly figures for the dashboard: the headline FASR/RTO/NDR trend line,
the order-volume bars beneath it, and the per-group comparison bar chart
used in the Payment / Delivery / Channel tabs.
"""

import pandas as pd
import plotly.graph_objects as go

COLOR_FASR = "#15803d"
COLOR_RTO = "#b91c1c"
COLOR_NDR = "#b45309"
COLOR_VOLUME = "#64748b"

LAYOUT_DEFAULTS = dict(
    plot_bgcolor="white",
    paper_bgcolor="white",
    font=dict(family="-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif", size=12, color="#1e293b"),
    margin=dict(t=36, l=10, r=10, b=10),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    hovermode="x unified",
)


def trend_chart(overall_wide_table: pd.DataFrame, height: int = 320) -> go.Figure:
    """overall_wide_table: rows = FASR %/RTO in 1st Att %/NDR %/Orders, columns = dates."""
    t = overall_wide_table.T
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t.index, y=t["FASR %"], name="FASR %", mode="lines+markers",
                              line=dict(color=COLOR_FASR, width=2.5)))
    fig.add_trace(go.Scatter(x=t.index, y=t["RTO in 1st Att %"], name="RTO %", mode="lines+markers",
                              line=dict(color=COLOR_RTO, width=2.5)))
    fig.add_trace(go.Scatter(x=t.index, y=t["NDR %"], name="NDR %", mode="lines+markers",
                              line=dict(color=COLOR_NDR, width=2.5)))
    fig.update_layout(
        **LAYOUT_DEFAULTS,
        height=height,
        yaxis=dict(tickformat=".0%", gridcolor="#eef1f6"),
        xaxis=dict(showgrid=False),
    )
    return fig


def volume_chart(overall_wide_table: pd.DataFrame, height: int = 150) -> go.Figure:
    t = overall_wide_table.T
    fig = go.Figure()
    fig.add_trace(go.Bar(x=t.index, y=t["Orders"], name="Orders", marker_color=COLOR_VOLUME))
    fig.update_layout(
        **{**LAYOUT_DEFAULTS, "showlegend": False},
        height=height,
        yaxis=dict(title="Orders", gridcolor="#eef1f6"),
        xaxis=dict(showgrid=False),
    )
    return fig


