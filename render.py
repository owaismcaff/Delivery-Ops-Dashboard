"""
render.py

Renders the wide (metric x date) tables as styled HTML instead of a flat
st.dataframe grid. Gives us things Streamlit's default table can't do:
  - a merged/rowspan cell for the group label (COD/PPD, or each courier),
    so each group reads as one clearly bounded block instead of the group
    name repeating on every row
  - sticky header row + sticky label columns while scrolling
  - color-coded metric rows (FASR green, RTO red, NDR amber) so the eye
    can track a metric across a wide, many-column table
"""

import html as _html

import pandas as pd

METRIC_CSS_CLASS = {
    "FASR %": "m-fasr",
    "RTO in 1st Att %": "m-rto",
    "RTO % (Overall)": "m-rto",
    "NDR %": "m-ndr",
    "NDR to Delivery %": "m-fasr",
    "NDR to RTO %": "m-rto",
    "NDR to Pending %": "m-ndr",
    "Orders": "m-orders",
    "1st Attempt: FASR %": "m-fasr",
    "1st Attempt: RTO %": "m-rto",
    "1st Attempt: Pending %": "m-ndr",
    "2nd Attempt: Delivered %": "m-fasr",
    "2nd Attempt: RTO %": "m-rto",
    "2nd Attempt: Pending %": "m-ndr",
    "3rd Attempt: Delivered %": "m-fasr",
    "3rd Attempt: RTO %": "m-rto",
    "3rd Attempt: Pending %": "m-ndr",
    "3+ Attempts: Delivered %": "m-fasr",
    "3+ Attempts: RTO %": "m-rto",
    "3+ Attempts: Pending %": "m-ndr",
    "Delivered % (Overall)": "m-fasr",
    "2nd Att Del %": "m-days",
    "3rd Att Del %": "m-days",
    "3+ Att Del %": "m-days",
    # Bare (un-prefixed) versions of the attempt sub-metrics, used by
    # Attempt Analysis Overall's "double block" layout - the Group column
    # already says "1st Attempt"/"2nd Attempt"/etc, so the Metric column
    # drops that prefix ("1st Attempt: RTO %" -> "RTO %") and needs its own
    # color lookup entry rather than falling through to no color.
    "RTO %": "m-rto",
    "Delivered %": "m-fasr",
    "Pending %": "m-ndr",
}

TABLE_CSS = """
<style>
.dash-table-wrap {
    background: #ffffff;
    overflow: auto;
    max-height: 640px;
    border-radius: 14px;
    border: 1px solid #e2e8f0;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 1px 3px rgba(15,23,42,0.06);
    margin: 0.25rem 0 1.75rem 0;
}
table.dash-table {
    border-collapse: separate;
    border-spacing: 0;
    width: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
}
.dash-table th, .dash-table td {
    padding: 9px 16px;
    white-space: nowrap;
}
.dash-table thead th {
    background: #0f172a;
    color: #f8fafc;
    position: sticky;
    top: 0;
    z-index: 3;
    text-align: right;
    font-weight: 600;
    font-size: 12.5px;
    border-bottom: 1px solid #0f172a;
}
.dash-table thead th.label-col { text-align: left; z-index: 5; }
.dash-table td.group-col, .dash-table th.group-col {
    position: sticky;
    left: 0;
    width: 140px;
    min-width: 140px;
    z-index: 2;
}
.dash-table td.metric-col, .dash-table th.metric-col {
    position: sticky;
    left: 140px;
    width: 170px;
    min-width: 170px;
    z-index: 2;
}
.dash-table.no-group td.metric-col, .dash-table.no-group th.metric-col {
    left: 0;
}
.dash-table thead th.group-col, .dash-table thead th.metric-col { z-index: 6; }
.dash-table td.label-col {
    background: #f8fafc;
    font-weight: 600;
    color: #334155;
    text-align: left;
    border-right: 1px solid #e2e8f0;
}
.dash-table td.date-col {
    text-align: right;
    color: #1e293b;
    border-bottom: 1px solid #eef1f6;
    border-right: 1px solid #e8edf3;
    font-variant-numeric: tabular-nums;
}
.dash-table th.date-col { border-right: 1px solid #e8edf3; }
.dash-table th.date-col:last-child, .dash-table td.date-col:last-child { border-right: none; }
/* Marks the last sub-column of each date's metric group (2D Cut, where
   several metrics sit side by side under one date) with a heavier line, so
   each date reads as one sealed-off block instead of metrics from
   neighboring dates blurring together. */
.dash-table td.date-end, .dash-table th.date-end { border-right: 2px solid #94a3b8; }
.dash-table tbody tr:hover td.date-col { background-color: #f0f5ff; }
.dash-table tr.group-start td { border-top: 2px solid #cbd5e1; }
.dash-table td.m-fasr { color: #15803d; }
.dash-table td.m-rto { color: #b91c1c; }
.dash-table td.m-ndr { color: #b45309; }
.dash-table td.m-orders { color: #475569; font-weight: 500; }
.dash-table td.m-days { color: #4338ca; font-weight: 500; }
.dash-table td.label-col.m-fasr { color: #15803d; }
.dash-table td.label-col.m-rto { color: #b91c1c; }
.dash-table td.label-col.m-ndr { color: #b45309; }
/* Header sub-columns (2D Cut's Orders/FASR%/RTO%/... row) sit on the dark
   navy header background, so the data cells' dark saturated colors above
   would be nearly invisible there - these are softer, muted, light
   versions of the same FASR/RTO/NDR/Orders palette, just readable on
   navy instead of white. */
.dash-table thead th.sub-col.m-fasr { color: #86efac; }
.dash-table thead th.sub-col.m-rto { color: #fca5a5; }
.dash-table thead th.sub-col.m-ndr { color: #fcd34d; }
.dash-table thead th.sub-col.m-orders { color: #cbd5e1; }
.dash-table thead th.sub-col.m-days { color: #a5b4fc; }
.dash-table td.label-col.m-days { color: #4338ca; }
.dash-table-2row-header thead tr:nth-child(2) th { top: 34px; }
.dash-table-2row-header thead tr:first-child th.date-col { text-align: center; }
.dash-table th.sub-col { font-weight: 500; font-size: 11.5px; opacity: 0.85; }
</style>
"""


def _esc(v) -> str:
    return _html.escape(str(v))


def table_html(
    display_table: pd.DataFrame, has_group: bool,
    group_label: str = "Group", metric_label: str = "Metric", fixed_metric_class: str | None = None,
) -> str:
    """display_table must already be string-formatted (see
    pipeline.format_wide_for_display). Returns a self-contained scrollable
    HTML table; call inject_css() once beforehand to load the stylesheet.
    group_label/metric_label override the header text for a has_group table
    whose two index levels aren't actually (Group, Metric) - e.g. City x
    Courier Cut's (City, Courier). fixed_metric_class, when given, is used
    for every row instead of looking the row label up in METRIC_CSS_CLASS
    (which only knows real metric names) - used by the FASR-only cuts, where
    every row is the same metric (FASR %) so every row gets the same
    green."""
    cols = list(display_table.columns)
    date_headers = "".join(f'<th class="date-col">{_esc(c)}</th>' for c in cols)

    if has_group:
        header = (
            f'<tr><th class="label-col group-col">{_esc(group_label)}</th>'
            f'<th class="label-col metric-col">{_esc(metric_label)}</th>{date_headers}</tr>'
        )
        groups = display_table.index.get_level_values(0)
        metrics = display_table.index.get_level_values(1)
        n = len(display_table)
        body_rows = []
        i = 0
        while i < n:
            grp = groups[i]
            span = 1
            while i + span < n and groups[i + span] == grp:
                span += 1
            for j in range(span):
                metric = metrics[i + j]
                cls = fixed_metric_class if fixed_metric_class is not None else METRIC_CSS_CLASS.get(metric, "")
                row_vals = display_table.iloc[i + j]
                tds = "".join(f'<td class="date-col {cls}">{_esc(v)}</td>' for v in row_vals)
                group_cell = (
                    f'<td class="label-col group-col" rowspan="{span}">{_esc(grp)}</td>' if j == 0 else ""
                )
                row_class = ' class="group-start"' if j == 0 else ""
                body_rows.append(
                    f'<tr{row_class}>{group_cell}'
                    f'<td class="label-col metric-col {cls}">{_esc(metric)}</td>{tds}</tr>'
                )
            i += span
        table_class = "dash-table"
    else:
        header = f'<tr><th class="label-col metric-col">{_esc(metric_label)}</th>{date_headers}</tr>'
        body_rows = []
        for metric, row in display_table.iterrows():
            cls = fixed_metric_class if fixed_metric_class is not None else METRIC_CSS_CLASS.get(metric, "")
            tds = "".join(f'<td class="date-col {cls}">{_esc(v)}</td>' for v in row)
            body_rows.append(f'<tr><td class="label-col metric-col {cls}">{_esc(metric)}</td>{tds}</tr>')
        table_class = "dash-table no-group"

    return (
        f'<div class="dash-table-wrap"><table class="{table_class}">'
        f'<thead>{header}</thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def grouped_orders_fasr_table_html(display_table: pd.DataFrame, row_label: str, subrow_label: str) -> str:
    """Renders a City x Courier Cut / State x Courier Cut table (see
    pipeline.group_orders_fasr_wide): rows are a (State/City, Courier)
    MultiIndex - rowspan-grouped by the first level, same idea as
    table_html()'s has_group path - but columns are a (date, metric)
    MultiIndex (Orders, FASR % per date) instead of a flat date index, so
    this needs its own 2-row header (date spanning 2 columns, then an
    Orders/FASR % sub-header) rather than reusing table_html()."""
    dates = list(dict.fromkeys(d for d, _ in display_table.columns))
    # colspan must match however many metric sub-columns actually fall under
    # each date - the 2D Cut tab lets the user pick any number of metrics
    # (not just a fixed Orders+FASR% pair), so a hardcoded colspan="2" would
    # silently misalign every header cell after the first date as soon as a
    # different metric count was selected.
    cols_per_date = {}
    for d, _ in display_table.columns:
        cols_per_date[d] = cols_per_date.get(d, 0) + 1
    date_header = "".join(f'<th class="date-col date-end" colspan="{cols_per_date[d]}">{_esc(d)}</th>' for d in dates)
    # Sub-header text is tinted per metric (same FASR/RTO/NDR/Orders classes
    # the data cells below already use) instead of plain white-on-navy, so
    # the header visually connects to its column's data and different
    # metrics are distinguishable at a glance across a wide, many-date
    # table. The last metric column of each date also gets "date-end" (a
    # heavier border), so each date's whole block of metrics reads as one
    # sealed-off column group instead of blurring into the next date.
    cols_list = list(display_table.columns)
    metric_header = "".join(
        f'<th class="date-col sub-col {METRIC_CSS_CLASS.get(m, "")}'
        f'{" date-end" if (i == len(cols_list) - 1 or cols_list[i + 1][0] != d) else ""}">{_esc(m)}</th>'
        for i, (d, m) in enumerate(cols_list)
    )
    header = (
        f'<tr><th class="label-col group-col"></th><th class="label-col metric-col"></th>{date_header}</tr>'
        f'<tr><th class="label-col group-col">{_esc(row_label)}</th>'
        f'<th class="label-col metric-col">{_esc(subrow_label)}</th>{metric_header}</tr>'
    )

    groups = display_table.index.get_level_values(0)
    subgroups = display_table.index.get_level_values(1)
    n = len(display_table)
    body_rows = []
    i = 0
    while i < n:
        grp = groups[i]
        span = 1
        while i + span < n and groups[i + span] == grp:
            span += 1
        for j in range(span):
            subgrp = subgroups[i + j]
            row_vals = display_table.iloc[i + j]
            tds = []
            for k, ((d, metric), v) in enumerate(zip(cols_list, row_vals)):
                cls = METRIC_CSS_CLASS.get(metric, "m-fasr")
                end_cls = " date-end" if (k == len(cols_list) - 1 or cols_list[k + 1][0] != d) else ""
                tds.append(f'<td class="date-col {cls}{end_cls}">{_esc(v)}</td>')
            group_cell = (
                f'<td class="label-col group-col" rowspan="{span}">{_esc(grp)}</td>' if j == 0 else ""
            )
            row_class = ' class="group-start"' if j == 0 else ""
            body_rows.append(
                f'<tr{row_class}>{group_cell}'
                f'<td class="label-col metric-col">{_esc(subgrp)}</td>{"".join(tds)}</tr>'
            )
        i += span

    return (
        f'<div class="dash-table-wrap"><table class="dash-table dash-table-2row-header">'
        f'<thead>{header}</thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    )
