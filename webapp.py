"""
webapp.py

Flask replacement for app.py - server-rendered HTML pages instead of
Streamlit. pipeline.py and render.py are reused completely unchanged (they
were already decoupled from Streamlit). Filters live in the URL query
string; each page/tab is its own route, so (unlike Streamlit, which reran
every tab's computation on every interaction) only the tab actually being
viewed gets computed.

Run with:
    python webapp.py
Then open http://localhost:5000
"""

import functools
import gc
import os
import secrets
from datetime import timedelta

import pandas as pd
from flask import Flask, Response, redirect, render_template, request, session, url_for

import charts as ch
import pipeline as pl
import render as rd

app = Flask(__name__)
# A fresh random key each process start is fine here - it only means admin
# sessions don't survive a server restart (same as having to log in again
# after any reboot), not a real security gap for this single-admin tool.
# Set FASR_SECRET_KEY explicitly if session persistence across restarts ever
# matters (e.g. once this runs behind a process manager that restarts it).
app.secret_key = os.environ.get("FASR_SECRET_KEY") or secrets.token_hex(32)

# No hardcoded fallback password - if FASR_ADMIN_PASSWORD isn't set, admin
# login is simply unavailable (see admin_login()) rather than silently
# accepting some default/blank password.
ADMIN_PASSWORD = os.environ.get("FASR_ADMIN_PASSWORD")


def admin_required(view_func):
    """Gates Upload/Backup/Restore/Reset - the only actions that modify or
    destroy stored data - behind a single shared admin password (see
    ADMIN_PASSWORD). Viewing the dashboard itself stays open to anyone with
    the link; this only protects the data-management actions."""
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped

_cache = {"mtime": None, "cube": None, "city_cube": None, "pincode_cube": None, "lane_cube": None}


def get_cube() -> pd.DataFrame:
    """The pre-aggregated cube (see pipeline.build_cube), cached on the
    store file's mtime - only rebuilt when the store itself changes, not
    per request. The raw per-order dataframe (5M+ rows, with_metrics()
    applied) is loaded only transiently here to build the cube (and the
    city_cube/pincode_cube alongside it, from the same pass), then dropped
    - it used to be kept resident for the whole process, which was most of
    this app's ~2.5-3.5GB standing memory footprint. Now only the much
    smaller cubes stay in memory.

    A plain server restart (no new data uploaded) used to pay the FULL
    rebuild cost again - all 3 groupbys over the 5.3M-row store, Pincode's
    alone being genuinely slow at tens of thousands of distinct values -
    even though the store file hadn't actually changed since the cubes were
    last built. load_cube_cache()/save_cube_cache() skip that: if a disk
    cache exists and matches this exact store mtime, the 3 (much smaller)
    cube files are read back directly instead of regrouping the raw store."""
    mtime = os.path.getmtime(pl.STORE_PATH) if pl.STORE_PATH.exists() else -1.0
    if _cache["mtime"] != mtime:
        cached = pl.load_cube_cache(mtime)
        if cached is not None:
            _cache["cube"], _cache["city_cube"], _cache["pincode_cube"], _cache["lane_cube"] = cached
        else:
            store = pl.load_store()
            full_df = pl.with_metrics(store)
            _cache["cube"] = pl.build_cube(full_df)
            _cache["city_cube"] = pl.build_city_cube(full_df)
            _cache["pincode_cube"] = pl.build_pincode_cube(full_df)
            _cache["lane_cube"] = pl.build_lane_cube(full_df)
            pl.save_cube_cache(_cache["cube"], _cache["city_cube"], _cache["pincode_cube"], _cache["lane_cube"], mtime)
            # store/full_df (5M+ rows, ~30 columns at peak) are only ever
            # needed transiently, right here, to build the 3 cubes - but
            # merely letting them go out of scope isn't enough on Windows:
            # CPython's/the OS's allocator often keeps those freed pages
            # reserved in the process rather than handing them back, so the
            # process's reported memory can stay near its PEAK (raw data +
            # all 3 cubes coexisting) indefinitely even though the actual
            # live cube data afterward is a fraction of that. Explicit del +
            # gc.collect() makes CPython release its references immediately
            # instead of waiting for the next GC cycle, which is what
            # actually lets the OS reclaim the large numpy-backed arrays
            # behind them right away instead of "eventually, maybe".
            del store, full_df
            gc.collect()
        # Pays the one-time "first groupby on this column" cost now, while
        # the cube build/load is already expected to take a moment -
        # without this it silently lands on whichever request first picks
        # City or Pincode in the 1D/2D Cut tabs instead.
        pl.warm_up_cube(_cache["cube"])
        pl.warm_up_cube(_cache["city_cube"], extra_col="City")
        pl.warm_up_cube(_cache["pincode_cube"], extra_col="Pincode")
        pl.warm_up_cube(_cache["lane_cube"], extra_col="Lane")
        _cache["mtime"] = mtime
    return _cache["cube"]


def get_city_cube() -> pd.DataFrame:
    """The cube with City as an extra dimension (see
    pipeline.build_city_cube), used by City Cut / City x Courier Cut and
    the unified 1D/2D Cut tabs when City is the picked dimension."""
    get_cube()  # ensures every cube is populated/fresh
    return _cache["city_cube"]


def get_pincode_cube() -> pd.DataFrame:
    """The cube with Pincode as an extra dimension (see
    pipeline.build_pincode_cube), used by the unified 1D/2D Cut tabs when
    Pincode is the picked dimension."""
    get_cube()
    return _cache["pincode_cube"]


def get_lane_cube() -> pd.DataFrame:
    """The cube with Lane as an extra dimension (see
    pipeline.build_lane_cube), used by the unified 1D/2D Cut tabs when
    Lane is the picked dimension."""
    get_cube()
    return _cache["lane_cube"]


def upload_log_html() -> str | None:
    log = pl.load_upload_log()
    if log.empty:
        return None
    log = log.sort_values("uploaded_at", ascending=False).head(15)
    cols = ["uploaded_at", "filename", "new_orders_added", "existing_orders_updated"]
    cols = [c for c in cols if c in log.columns]
    rows = "".join(
        "<tr>" + "".join(f"<td>{row[c]}</td>" for c in cols) + "</tr>"
        for _, row in log.iterrows()
    )
    header = "".join(f"<th>{c}</th>" for c in cols)
    return f'<table class="upload-log-table"><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>'


def parse_filters(cube: pd.DataFrame) -> dict:
    all_dates = cube["first_ofd_date"]
    # A genuinely empty store (e.g. right after Reset, before any upload)
    # has an empty cube - .min()/.max() on it return NaN, not NaT, so
    # .date() crashes. Fall back to today for both so the dashboard still
    # renders (showing 0 orders) instead of a 500, letting the user
    # upload or restore from backup.
    if cube.empty:
        today = pd.Timestamp.now().normalize().date()
        data_min, data_max = today, today
    else:
        data_min, data_max = all_dates.min().date(), all_dates.max().date()

    view = request.args.get("view", "day")
    granularity = view

    # Default to the trailing 30 days (ending on the latest data date), not
    # the full Jan-to-date range - otherwise the date picker's "Start" field
    # opens showing Jan every time, forcing a click-through of 9+ months to
    # reach a recent date. Only applies when start/end aren't already in the
    # URL, so explicit links/bookmarks still work as given.
    default_start = data_max - timedelta(days=29)
    if default_start < data_min:
        default_start = data_min
    start = request.args.get("start") or str(default_start)
    end = request.args.get("end") or str(data_max)
    range_start = pd.Timestamp(start)
    range_end = pd.Timestamp(end)

    all_brands = pl.discover_brands(cube)
    all_channels = pl.discover_channels(cube)
    all_payment_modes = pl.discover_payment_modes(cube)
    all_states = pl.discover_states(cube)
    all_statuses = pl.discover_statuses(cube)
    all_couriers = pl.discover_couriers_alpha(cube)

    selected_brands = request.args.getlist("brand") or all_brands
    selected_channels = request.args.getlist("channel") or all_channels
    selected_payment_modes = request.args.getlist("payment") or all_payment_modes
    selected_states = request.args.getlist("state") or all_states
    selected_statuses = request.args.getlist("status") or all_statuses
    selected_couriers = request.args.getlist("courier") or all_couriers

    # Every cut is computed from the pre-aggregated cube - no raw per-order
    # data is filtered per request (that used to cost ~12s alone before
    # Pincode Cut, the one tab that needed row-level data, was dropped).
    # The cube has no NaN first_ofd_date rows to begin with (dropped when
    # built), so no isna() branch is needed here. Courier is filterable
    # everywhere (main/city/pincode cubes all carry Grouped_Courier_v2,
    # unlike City - see pipeline.DIMENSIONS' "source" field - so no
    # per-cube special-casing is needed for it).
    if selected_brands:
        cube = cube[cube["Brand"].isin(selected_brands)]
    if selected_channels:
        cube = cube[cube["channel_name"].isin(selected_channels)]
    if selected_payment_modes:
        cube = cube[cube["Payment mode"].isin(selected_payment_modes)]
    if selected_states:
        cube = cube[cube["State"].isin(selected_states)]
    if selected_statuses:
        cube = cube[cube["Final Status2"].isin(selected_statuses)]
    if selected_couriers:
        cube = cube[cube["Grouped_Courier_v2"].isin(selected_couriers)]
    cube_attr = cube
    cube_df = cube[(cube["first_ofd_date"] >= range_start) & (cube["first_ofd_date"] <= range_end)]

    # Same filters applied to the city cube - only City Cut / City x Courier
    # Cut use this, but filtering it here (rather than lazily) keeps this
    # function's shape consistent with cube_df above, and the city cube is
    # cheap enough (lean, 2 summed columns) that filtering it unconditionally
    # costs nothing worth special-casing.
    city_cube = get_city_cube()
    if selected_brands:
        city_cube = city_cube[city_cube["Brand"].isin(selected_brands)]
    if selected_channels:
        city_cube = city_cube[city_cube["channel_name"].isin(selected_channels)]
    if selected_payment_modes:
        city_cube = city_cube[city_cube["Payment mode"].isin(selected_payment_modes)]
    if selected_states:
        city_cube = city_cube[city_cube["State"].isin(selected_states)]
    if selected_statuses:
        city_cube = city_cube[city_cube["Final Status2"].isin(selected_statuses)]
    if selected_couriers:
        city_cube = city_cube[city_cube["Grouped_Courier_v2"].isin(selected_couriers)]
    city_cube_attr = city_cube
    city_cube_df = city_cube[(city_cube["first_ofd_date"] >= range_start) & (city_cube["first_ofd_date"] <= range_end)]

    # Same filters applied to the pincode cube - only used by the unified
    # 1D/2D Cut tabs when Pincode is the picked dimension.
    pincode_cube = get_pincode_cube()
    if selected_brands:
        pincode_cube = pincode_cube[pincode_cube["Brand"].isin(selected_brands)]
    if selected_channels:
        pincode_cube = pincode_cube[pincode_cube["channel_name"].isin(selected_channels)]
    if selected_payment_modes:
        pincode_cube = pincode_cube[pincode_cube["Payment mode"].isin(selected_payment_modes)]
    if selected_states:
        pincode_cube = pincode_cube[pincode_cube["State"].isin(selected_states)]
    if selected_statuses:
        pincode_cube = pincode_cube[pincode_cube["Final Status2"].isin(selected_statuses)]
    if selected_couriers:
        pincode_cube = pincode_cube[pincode_cube["Grouped_Courier_v2"].isin(selected_couriers)]
    pincode_cube_attr = pincode_cube
    pincode_cube_df = pincode_cube[(pincode_cube["first_ofd_date"] >= range_start) & (pincode_cube["first_ofd_date"] <= range_end)]

    # Same filters applied to the lane cube - only used by the unified
    # 1D/2D Cut tabs when Lane is the picked dimension.
    lane_cube = get_lane_cube()
    if selected_brands:
        lane_cube = lane_cube[lane_cube["Brand"].isin(selected_brands)]
    if selected_channels:
        lane_cube = lane_cube[lane_cube["channel_name"].isin(selected_channels)]
    if selected_payment_modes:
        lane_cube = lane_cube[lane_cube["Payment mode"].isin(selected_payment_modes)]
    if selected_states:
        lane_cube = lane_cube[lane_cube["State"].isin(selected_states)]
    if selected_statuses:
        lane_cube = lane_cube[lane_cube["Final Status2"].isin(selected_statuses)]
    if selected_couriers:
        lane_cube = lane_cube[lane_cube["Grouped_Courier_v2"].isin(selected_couriers)]
    lane_cube_attr = lane_cube
    lane_cube_df = lane_cube[(lane_cube["first_ofd_date"] >= range_start) & (lane_cube["first_ofd_date"] <= range_end)]

    period_days = (range_end - range_start).days + 1
    prev_end = range_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)

    qs = {
        "view": view, "start": range_start.date().isoformat(), "end": range_end.date().isoformat(),
        "brand": selected_brands, "channel": selected_channels, "payment": selected_payment_modes,
        "state": selected_states, "status": selected_statuses, "courier": selected_couriers,
    }

    return dict(
        view=view, granularity=granularity, data_min=data_min, data_max=data_max,
        range_start=range_start, range_end=range_end,
        all_brands=all_brands, selected_brands=selected_brands,
        all_channels=all_channels, selected_channels=selected_channels,
        all_payment_modes=all_payment_modes, selected_payment_modes=selected_payment_modes,
        all_states=all_states, selected_states=selected_states,
        all_statuses=all_statuses, selected_statuses=selected_statuses,
        all_couriers=all_couriers, selected_couriers=selected_couriers,
        cube_attr=cube_attr, cube_df=cube_df,
        city_cube_attr=city_cube_attr, city_cube_df=city_cube_df,
        pincode_cube_attr=pincode_cube_attr, pincode_cube_df=pincode_cube_df,
        lane_cube_attr=lane_cube_attr, lane_cube_df=lane_cube_df,
        period_days=period_days, prev_start=prev_start, prev_end=prev_end,
        qs=qs,
    )


def _date_filtered(attr_df, cut_start, cut_end):
    """attr_df (already filtered by every sidebar filter except date)
    narrowed to [cut_start, cut_end] - the 1D/2D Cut tabs' OWN date range,
    independent of the sidebar's. cut_start/cut_end are pd.Timestamp or
    None (None on either side = don't bound that side, matching how the
    cut date fields behave if left blank)."""
    df = attr_df
    if cut_start is not None:
        df = df[df["first_ofd_date"] >= cut_start]
    if cut_end is not None:
        df = df[df["first_ofd_date"] <= cut_end]
    return df


def _parse_search_terms() -> list[str] | None:
    """The 1D/2D Cut tabs' "Search" field - comma-separated values (e.g.
    specific pincodes) to look up directly, bypassing both min_orders and
    the City/Pincode row cap (see pl.discover_groups_above_threshold /
    pl.discover_grouped_rows's search_terms param) - someone who already
    knows exactly which Pincode/City they want shouldn't have to hope it
    lands in the top 300 by volume. None when the field is empty."""
    raw = request.args.get("dim_search", "").strip()
    if not raw:
        return None
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    return terms or None


def _no_match_note(search_terms: list[str], label: str) -> str:
    return f'No {label} values match "{", ".join(search_terms)}".'


def _cut_extra_qs(dim_key, selected_metrics, dim1_key, dim2_key, selected_metrics_2d,
                   min_orders, sort_by, cut_start, cut_end, dim_search_raw) -> dict:
    """Everything that defines the current 1D/2D Cut selection, folded into
    the shared `qs` dict so every link built with **qs (sub-tab links,
    top-tab links) and the sidebar's own filter form (base.html, via hidden
    inputs mirroring qs) carry it forward automatically. Without this, the
    cut only ever lived in this one page's URL - switching sub-tabs or
    applying any sidebar filter silently dropped the whole selection,
    forcing it to be re-entered from scratch every time."""
    return {
        "dim": dim_key, "metric": selected_metrics,
        "dim1": dim1_key, "dim2": dim2_key, "metric2": selected_metrics_2d,
        "min_orders": min_orders, "sort": sort_by,
        "cut_start": cut_start.date().isoformat(), "cut_end": cut_end.date().isoformat(),
        "dim_search": dim_search_raw,
    }


def _row_cap_note(cap: int, total: int, label: str) -> str:
    return (f"Showing top {cap:,} of {total:,} {label} values by order volume - "
            f"narrow with sidebar filters or raise Min orders to see fewer.")


_HIGH_CARDINALITY_COLUMNS = ("City", "Pincode", "Lane")


def _cap_groups(groups: list, col: str, label: str) -> tuple[list, str | None]:
    """Applies pl.ROW_CAP_FOR_HIGH_CARDINALITY to the 1D Cut tab's already-
    ranked (by volume, descending) group list, only for City/Pincode/Lane -
    see pl.ROW_CAP_FOR_HIGH_CARDINALITY's docstring for why. Returns
    (possibly truncated groups, a note to show the user when truncated,
    else None)."""
    if col not in _HIGH_CARDINALITY_COLUMNS or len(groups) <= pl.ROW_CAP_FOR_HIGH_CARDINALITY:
        return groups, None
    cap = pl.ROW_CAP_FOR_HIGH_CARDINALITY
    return groups[:cap], _row_cap_note(cap, len(groups), label)


def _resolve_dimension(dim_key: str, f: dict, cut_start=None, cut_end=None):
    """(src_df, column, label) for a pipeline.DIMENSIONS key - or None if
    dim_key isn't a recognized dimension. Used by the unified 1D/2D Cut
    tabs so the same dispatch logic works whether the picked dimension
    lives in the main cube (State/Payment/Courier/Channel - free) or its
    own cube (City/Pincode). cut_start/cut_end, when given, override the
    sidebar's date range for this cut only (see _date_filtered); omitted,
    this falls back to the sidebar-date-filtered cube_df/city_cube_df/
    pincode_cube_df exactly as before."""
    dim = pl.DIMENSIONS.get(dim_key)
    if not dim:
        return None
    source = dim["source"]
    if cut_start is not None or cut_end is not None:
        attr_df = {
            "main": f["cube_attr"], "city": f["city_cube_attr"],
            "pincode": f["pincode_cube_attr"], "lane": f["lane_cube_attr"],
        }[source]
        src_df = _date_filtered(attr_df, cut_start, cut_end)
    else:
        src_df = {
            "main": f["cube_df"], "city": f["city_cube_df"],
            "pincode": f["pincode_cube_df"], "lane": f["lane_cube_df"],
        }[source]
    return src_df, dim["column"], dim["label"]


def _resolve_2d_dimensions(dim1_key: str, dim2_key: str, f: dict, cut_start=None, cut_end=None):
    """(src_df, outer_col, outer_label, inner_col, inner_label) for the 2D
    Cut tab - the picked pair, auto-ordered so whichever dimension has more
    distinct values in the CURRENT (filtered) data becomes the outer/
    rowspan group, regardless of which slot the user put it in. Returns
    None if either key is unrecognized or they're the same dimension
    twice; "unsupported" if both are geography dimensions (City/Pincode/
    Lane, see pl.GEO_DIMENSION_SOURCES) - no cube carries more than one of
    them at once, and each implies the other anyway (Lane implies Pincode
    implies City). cut_start/cut_end behave as in _resolve_dimension."""
    d1, d2 = pl.DIMENSIONS.get(dim1_key), pl.DIMENSIONS.get(dim2_key)
    if not d1 or not d2 or dim1_key == dim2_key:
        return None
    sources = {d1["source"], d2["source"]}
    if len(sources & pl.GEO_DIMENSION_SOURCES) > 1:
        return "unsupported"
    source = next(iter(sources & pl.GEO_DIMENSION_SOURCES), "main")
    if cut_start is not None or cut_end is not None:
        attr_df = {
            "main": f["cube_attr"], "city": f["city_cube_attr"],
            "pincode": f["pincode_cube_attr"], "lane": f["lane_cube_attr"],
        }[source]
        src_df = _date_filtered(attr_df, cut_start, cut_end)
    else:
        src_df = {
            "main": f["cube_df"], "city": f["city_cube_df"],
            "pincode": f["pincode_cube_df"], "lane": f["lane_cube_df"],
        }[source]
    n1 = src_df[d1["column"]].nunique()
    n2 = src_df[d2["column"]].nunique()
    if n1 >= n2:
        return src_df, d1["column"], d1["label"], d2["column"], d2["label"]
    return src_df, d2["column"], d2["label"], d1["column"], d1["label"]


def _delta(cur, prev):
    if cur is None or prev is None:
        return None
    return cur - prev


def build_kpi(cube_attr, f):
    current = pl.aggregate_metrics_cube(cube_attr, f["range_start"], f["range_end"])
    previous = pl.aggregate_metrics_cube(cube_attr, f["prev_start"], f["prev_end"])

    def fmt(key, inverse):
        cur, prev = current[key], previous[key]
        val = f"{cur*100:.1f}%" if cur is not None else "-"
        d = _delta(cur, prev)
        if d is None:
            return val, "", ""
        good = (d < 0) if inverse else (d > 0)
        cls = "good" if good else ("bad" if d != 0 else "")
        return val, f"{d*100:+.1f}pp", cls

    fasr, fasr_d, fasr_c = fmt("FASR %", inverse=False)
    rto1, rto1_d, rto1_c = fmt("RTO %", inverse=True)
    rto_o, rto_o_d, rto_o_c = fmt("RTO % (Overall)", inverse=True)
    ndr, ndr_d, ndr_c = fmt("NDR %", inverse=True)

    return dict(
        orders=f"{current['Orders']:,}",
        fasr=fasr, fasr_delta=fasr_d, fasr_delta_class=fasr_c,
        rto1=rto1, rto1_delta=rto1_d, rto1_delta_class=rto1_c,
        rto_overall=rto_o, rto_overall_delta=rto_o_d, rto_overall_delta_class=rto_o_c,
        ndr=ndr, ndr_delta=ndr_d, ndr_delta_class=ndr_c,
        period_days=f["period_days"], prev_start=f["prev_start"].date(), prev_end=f["prev_end"].date(),
    )


def _base_ctx(f, top_tab, extra_qs=None):
    return dict(
        top_tab=top_tab,
        view=f["view"], range_start=f["range_start"].date().isoformat(), range_end=f["range_end"].date().isoformat(),
        data_min=f["data_min"].isoformat(), data_max=f["data_max"].isoformat(),
        all_brands=f["all_brands"], selected_brands=f["selected_brands"],
        all_channels=f["all_channels"], selected_channels=f["selected_channels"],
        all_payment_modes=f["all_payment_modes"], selected_payment_modes=f["selected_payment_modes"],
        all_states=f["all_states"], selected_states=f["selected_states"],
        all_statuses=f["all_statuses"], selected_statuses=f["selected_statuses"],
        all_couriers=f["all_couriers"], selected_couriers=f["selected_couriers"],
        table_css=rd.TABLE_CSS,
        upload_log_html=upload_log_html(), flash=request.args.get("flash"),
        backup_timestamp=pl.backup_timestamp(),
        qs={**f["qs"], **(extra_qs or {})},
    )


@app.route("/")
def index():
    return redirect(url_for("dashboard", subtab="overall"))


@app.route("/dashboard/<subtab>")
def dashboard(subtab):
    cube = get_cube()
    if cube.empty:
        return render_template("empty.html", backup_timestamp=pl.backup_timestamp())
    f = parse_filters(cube)
    granularity = f["granularity"]
    cube_attr, cube_df = f["cube_attr"], f["cube_df"]

    overall_daily = pl.overall_wide_cube(cube_df, granularity="day")
    # responsive=True makes Plotly watch its container with a ResizeObserver -
    # without it, a chart only ever redraws on the browser window's own
    # resize event, so collapsing the sidebar (a pure CSS/flex layout change,
    # not a window resize) left charts stuck at their original width.
    plotly_config = {"responsive": True}
    trend_chart_html = ch.trend_chart(overall_daily).to_html(
        full_html=False, include_plotlyjs="cdn", config=plotly_config
    )
    volume_chart_html = ch.volume_chart(overall_daily).to_html(
        full_html=False, include_plotlyjs=False, config=plotly_config
    )

    sort_options = None
    sort_by = request.args.get("sort", "Default")
    min_orders_raw = request.args.get("min_orders", str(pl.CITY_CUT_DEFAULT_MIN_ORDERS))
    min_orders = int(min_orders_raw) if min_orders_raw.strip().lstrip("-").isdigit() else pl.CITY_CUT_DEFAULT_MIN_ORDERS
    show_min_orders = subtab in ("cut-1d", "cut-2d")

    # 1D/2D Cut extras - only meaningful for their own subtab, but computed
    # unconditionally (cheap) so _base_ctx/template always has consistent
    # values to work with regardless of which tab is being viewed.
    dim_key = request.args.get("dim", "")
    selected_metrics = request.args.getlist("metric")
    cut1d_generated = bool(dim_key) and bool(selected_metrics)
    dim1_key = request.args.get("dim1", "")
    dim2_key = request.args.get("dim2", "")
    selected_metrics_2d = request.args.getlist("metric2")
    cut2d_generated = bool(dim1_key) and bool(dim2_key) and bool(selected_metrics_2d)
    cut2d_error = None

    # The 1D/2D Cut tabs get their OWN date range, independent of the
    # sidebar's - defaults to matching the sidebar on first visit (so the
    # fields start somewhere sensible), then persists whatever the user
    # sets here without touching the sidebar/KPI cards/trend chart at all.
    cut_start = pd.Timestamp(request.args.get("cut_start") or f["range_start"].date().isoformat())
    cut_end = pd.Timestamp(request.args.get("cut_end") or f["range_end"].date().isoformat())

    row_cap_note = None
    dim_search_raw = request.args.get("dim_search", "")
    search_terms = _parse_search_terms()
    if subtab == "overall":
        table = pl.overall_wide_cube(cube_df, granularity)
        table_html = rd.table_html(pl.format_wide_for_display(table, granularity), has_group=False)
    elif subtab == "cut-1d":
        table_html = None
        if cut1d_generated:
            resolved = _resolve_dimension(dim_key, f, cut_start, cut_end)
            if resolved is None:
                return "Unknown dimension", 404
            src_df, col, label = resolved
            if search_terms:
                groups = pl.discover_groups_above_threshold(src_df, col, min_orders, search_terms=search_terms)
                row_cap_note = None if groups else _no_match_note(search_terms, label)
            else:
                groups, row_cap_note = _cap_groups(pl.discover_groups_above_threshold(src_df, col, min_orders), col, label)
            summary = pl.group_summary(src_df, col, groups, cut_start, cut_end)
            ordered = pl.sort_groups(summary, sort_by, groups)
            metrics_list = [m for m in pl.METRICS if m[0] in selected_metrics]
            include_orders = "Orders" in selected_metrics
            table = pl.grouped_wide_cube(src_df, col, ordered, granularity,
                                          metrics_list=metrics_list, include_orders=include_orders)
            table_html = rd.table_html(pl.format_wide_for_display(table, granularity), has_group=True, group_label=label)
            sort_options = pl.SORT_OPTIONS
    elif subtab == "cut-2d":
        table_html = None
        if cut2d_generated:
            resolved = _resolve_2d_dimensions(dim1_key, dim2_key, f, cut_start, cut_end)
            if resolved is None:
                return "Unknown or duplicate dimensions", 404
            if resolved == "unsupported":
                cut2d_error = "City/Pincode/Lane can't be combined with each other - pick a different pair (each one implies the others)."
            else:
                src_df, outer_col, outer_label, inner_col, inner_label = resolved
                row_cap = None if search_terms else (pl.ROW_CAP_FOR_HIGH_CARDINALITY if outer_col in _HIGH_CARDINALITY_COLUMNS else None)
                pairs, total_outer = pl.discover_grouped_rows(src_df, outer_col, inner_col, min_orders=min_orders,
                                                                sort_by=sort_by, max_outer_groups=row_cap,
                                                                search_terms=search_terms)
                if search_terms and not pairs:
                    row_cap_note = _no_match_note(search_terms, outer_label)
                elif row_cap is not None and total_outer > row_cap:
                    row_cap_note = _row_cap_note(row_cap, total_outer, outer_label)
                metrics_list = [m for m in pl.METRICS if m[0] in selected_metrics_2d]
                include_orders = "Orders" in selected_metrics_2d
                table = pl.group_orders_fasr_wide(src_df, outer_col, inner_col, pairs, granularity,
                                                   metrics_list=metrics_list, include_orders=include_orders)
                table_html = rd.grouped_orders_fasr_table_html(pl.format_orders_fasr_for_display(table, granularity),
                                                                 row_label=outer_label, subrow_label=inner_label)
                sort_options = pl.MATRIX_SORT_OPTIONS
    else:
        return "Unknown tab", 404

    extra_qs = _cut_extra_qs(dim_key, selected_metrics, dim1_key, dim2_key, selected_metrics_2d,
                              min_orders, sort_by, cut_start, cut_end, dim_search_raw)
    ctx = _base_ctx(f, "dashboard", extra_qs=extra_qs)
    ctx.update(
        subtab=subtab, kpi=build_kpi(cube_attr, f),
        trend_chart_html=trend_chart_html, volume_chart_html=volume_chart_html,
        sort_options=sort_options, sort_by=sort_by,
        show_min_orders=show_min_orders, min_orders=min_orders,
        cut_start=cut_start.date().isoformat(), cut_end=cut_end.date().isoformat(),
        table_html=table_html, row_cap_note=row_cap_note, dim_search=dim_search_raw,
        dimensions=pl.DIMENSIONS, selected_dim=dim_key,
        all_metric_labels=pl.SELECTABLE_METRIC_LABELS, selected_metrics=selected_metrics,
        cut1d_generated=cut1d_generated,
        selected_dim1=dim1_key, selected_dim2=dim2_key, selected_metrics_2d=selected_metrics_2d,
        cut2d_generated=cut2d_generated, cut2d_error=cut2d_error,
    )
    return render_template("dashboard.html", **ctx)


@app.route("/attempt-analysis/<subtab>")
def attempt_analysis(subtab):
    cube = get_cube()
    if cube.empty:
        return render_template("empty.html", backup_timestamp=pl.backup_timestamp())
    f = parse_filters(cube)
    granularity = f["granularity"]
    cube_df = f["cube_df"]

    sort_options = None
    sort_by = request.args.get("sort", "Default")
    min_orders_raw = request.args.get("min_orders", str(pl.CITY_CUT_DEFAULT_MIN_ORDERS))
    min_orders = int(min_orders_raw) if min_orders_raw.strip().lstrip("-").isdigit() else pl.CITY_CUT_DEFAULT_MIN_ORDERS
    show_min_orders = subtab in ("cut-1d", "cut-2d")

    dim_key = request.args.get("dim", "")
    selected_metrics = request.args.getlist("metric")
    cut1d_generated = bool(dim_key) and bool(selected_metrics)

    dim1_key = request.args.get("dim1", "")
    dim2_key = request.args.get("dim2", "")
    selected_metrics_2d = request.args.getlist("metric2")
    cut2d_generated = bool(dim1_key) and bool(dim2_key) and bool(selected_metrics_2d)
    cut2d_error = None

    cut_start = pd.Timestamp(request.args.get("cut_start") or f["range_start"].date().isoformat())
    cut_end = pd.Timestamp(request.args.get("cut_end") or f["range_end"].date().isoformat())

    # Attempt cascade metrics never involve averaging (unlike the removed
    # Average Delivery Days), so these all work unchanged on the cube - no
    # separate cube variants needed.
    row_cap_note = None
    dim_search_raw = request.args.get("dim_search", "")
    search_terms = _parse_search_terms()
    if subtab == "overall":
        table = pl.attempt_overall_grouped_wide(cube_df, granularity)
        table_html = rd.table_html(pl.format_wide_for_display(table, granularity), has_group=True, group_label="Attempt")
    elif subtab == "cut-1d":
        table_html = None
        if cut1d_generated:
            resolved = _resolve_dimension(dim_key, f, cut_start, cut_end)
            if resolved is None:
                return "Unknown dimension", 404
            src_df, col, label = resolved
            if search_terms:
                groups = pl.discover_groups_above_threshold(src_df, col, min_orders, search_terms=search_terms)
                row_cap_note = None if groups else _no_match_note(search_terms, label)
            else:
                groups, row_cap_note = _cap_groups(pl.discover_groups_above_threshold(src_df, col, min_orders), col, label)
            summary = pl.group_summary(src_df, col, groups, cut_start, cut_end)
            ordered = pl.sort_groups(summary, sort_by, groups)
            table = pl.attempt_grouped_wide(src_df, col, ordered, granularity, metrics_list=selected_metrics)
            table_html = rd.table_html(pl.format_wide_for_display(table, granularity), has_group=True, group_label=label)
            sort_options = pl.SORT_OPTIONS
    elif subtab == "cut-2d":
        table_html = None
        if cut2d_generated:
            resolved = _resolve_2d_dimensions(dim1_key, dim2_key, f, cut_start, cut_end)
            if resolved is None:
                return "Unknown or duplicate dimensions", 404
            if resolved == "unsupported":
                cut2d_error = "City/Pincode/Lane can't be combined with each other - pick a different pair (each one implies the others)."
            else:
                src_df, outer_col, outer_label, inner_col, inner_label = resolved
                row_cap = None if search_terms else (pl.ROW_CAP_FOR_HIGH_CARDINALITY if outer_col in _HIGH_CARDINALITY_COLUMNS else None)
                pairs, total_outer = pl.discover_grouped_rows(src_df, outer_col, inner_col, min_orders=min_orders,
                                                                sort_by=sort_by, max_outer_groups=row_cap,
                                                                search_terms=search_terms)
                if search_terms and not pairs:
                    row_cap_note = _no_match_note(search_terms, outer_label)
                elif row_cap is not None and total_outer > row_cap:
                    row_cap_note = _row_cap_note(row_cap, total_outer, outer_label)
                metrics_list = [m for m in pl.ATTEMPT_METRICS if m[0] in selected_metrics_2d]
                include_orders = "Orders" in selected_metrics_2d
                table = pl.group_orders_fasr_wide(src_df, outer_col, inner_col, pairs, granularity,
                                                   metrics_list=metrics_list, include_orders=include_orders)
                table_html = rd.grouped_orders_fasr_table_html(pl.format_orders_fasr_for_display(table, granularity),
                                                                 row_label=outer_label, subrow_label=inner_label)
                sort_options = pl.MATRIX_SORT_OPTIONS
    else:
        return "Unknown tab", 404

    extra_qs = _cut_extra_qs(dim_key, selected_metrics, dim1_key, dim2_key, selected_metrics_2d,
                              min_orders, sort_by, cut_start, cut_end, dim_search_raw)
    ctx = _base_ctx(f, "attempt", extra_qs=extra_qs)
    ctx.update(
        subtab=subtab, table_html=table_html, row_cap_note=row_cap_note, dim_search=dim_search_raw,
        sort_options=sort_options, sort_by=sort_by,
        show_min_orders=show_min_orders, min_orders=min_orders,
        cut_start=cut_start.date().isoformat(), cut_end=cut_end.date().isoformat(),
        dimensions=pl.DIMENSIONS, selected_dim=dim_key,
        all_metric_labels=pl.ATTEMPT_SELECTABLE_METRIC_LABELS, selected_metrics=selected_metrics,
        cut1d_generated=cut1d_generated,
        selected_dim1=dim1_key, selected_dim2=dim2_key,
        all_metric_labels_2d=pl.ATTEMPT_2D_SELECTABLE_METRIC_LABELS, selected_metrics_2d=selected_metrics_2d,
        cut2d_generated=cut2d_generated, cut2d_error=cut2d_error,
    )
    return render_template("attempt_analysis.html", **ctx)


@app.route("/download/<kind>")
def download(kind):
    cube = get_cube()
    f = parse_filters(cube)
    granularity = f["granularity"]
    cube_df = f["cube_df"]
    sort_by = request.args.get("sort", "Default")
    min_orders_raw = request.args.get("min_orders", str(pl.CITY_CUT_DEFAULT_MIN_ORDERS))
    min_orders = int(min_orders_raw) if min_orders_raw.strip().lstrip("-").isdigit() else pl.CITY_CUT_DEFAULT_MIN_ORDERS
    cut_start = pd.Timestamp(request.args.get("cut_start") or f["range_start"].date().isoformat())
    cut_end = pd.Timestamp(request.args.get("cut_end") or f["range_end"].date().isoformat())
    search_terms = _parse_search_terms()

    if kind == "overall":
        table = pl.format_wide_for_display(pl.overall_wide_cube(cube_df, granularity), granularity)
    elif kind == "cut-1d":
        dim_key = request.args.get("dim", "")
        selected_metrics = request.args.getlist("metric")
        resolved = _resolve_dimension(dim_key, f, cut_start, cut_end)
        if resolved is None or not selected_metrics:
            return "Nothing to export - pick a dimension and at least one metric first", 400
        src_df, col, _ = resolved
        groups = pl.discover_groups_above_threshold(src_df, col, min_orders, search_terms=search_terms)
        summary = pl.group_summary(src_df, col, groups, cut_start, cut_end)
        ordered = pl.sort_groups(summary, sort_by, groups)
        metrics_list = [m for m in pl.METRICS if m[0] in selected_metrics]
        include_orders = "Orders" in selected_metrics
        # grouped_wide_cube is fully vectorized (see pipeline.py) - even a
        # Lane export (60k+ distinct values) at Daily granularity finishes
        # in a few seconds, so no granularity override is needed here
        # (unlike the 2D export below, where the cost is sheer data volume
        # from crossing with a second dimension, not an algorithm issue).
        table = pl.format_wide_for_display(
            pl.grouped_wide_cube(src_df, col, ordered, granularity, metrics_list=metrics_list, include_orders=include_orders),
            granularity,
        )
    elif kind == "cut-2d":
        dim1_key = request.args.get("dim1", "")
        dim2_key = request.args.get("dim2", "")
        selected_metrics_2d = request.args.getlist("metric2")
        resolved = _resolve_2d_dimensions(dim1_key, dim2_key, f, cut_start, cut_end)
        if resolved is None or resolved == "unsupported" or not selected_metrics_2d:
            return "Nothing to export - pick two dimensions and at least one metric first", 400
        src_df, outer_col, _, inner_col, _ = resolved
        # Deliberately uncapped (max_outer_groups=None) - "download" means
        # the complete data, not the interactive view's top-N. A search
        # term (if the user had one set) still narrows the export, same as
        # the on-screen table, so the CSV matches what they were looking at.
        pairs, _ = pl.discover_grouped_rows(src_df, outer_col, inner_col, min_orders=min_orders, sort_by=sort_by,
                                             search_terms=search_terms)
        metrics_list = [m for m in pl.METRICS if m[0] in selected_metrics_2d]
        include_orders = "Orders" in selected_metrics_2d
        # Same reasoning as the cut-1d export above - outer_col is always
        # the higher-cardinality side of the pair (see _resolve_2d_
        # dimensions), so this check alone covers any City/Pincode/Lane
        # pairing regardless of which dropdown slot it was picked in.
        export_granularity = "month" if outer_col in _HIGH_CARDINALITY_COLUMNS else granularity
        table = pl.format_orders_fasr_for_display(
            pl.group_orders_fasr_wide(src_df, outer_col, inner_col, pairs, export_granularity,
                                       metrics_list=metrics_list, include_orders=include_orders),
            export_granularity,
        )
    else:
        return "Unknown export", 404

    csv_bytes = table.to_csv().encode("utf-8")
    return Response(csv_bytes, mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment;filename={kind}_{granularity}.csv"})


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    next_url = request.values.get("next") or url_for("dashboard", subtab="overall")
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            error = "Admin login isn't configured - set the FASR_ADMIN_PASSWORD environment variable and restart the server."
        elif secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["is_admin"] = True
            return redirect(next_url)
        else:
            error = "Wrong password."
    return render_template("admin_login.html", error=error, next_url=next_url)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("dashboard", subtab="overall"))


@app.route("/upload", methods=["POST"])
@admin_required
def upload():
    files = request.files.getlist("files")
    n = 0
    for f in files:
        if not f or not f.filename:
            continue
        cleaned, clean_stats = pl.clean_upload(f)
        merge_stats = pl.merge_into_store(cleaned)
        pl.append_upload_log(f.filename, clean_stats, merge_stats)
        n += 1
    flash = f"Merged {n} file(s)." if n else "No files uploaded."
    return redirect(url_for("dashboard", subtab="overall", flash=flash))


@app.route("/reset", methods=["POST"])
@admin_required
def reset():
    pl.reset_store()
    return redirect(url_for("dashboard", subtab="overall", flash="Store cleared."))


@app.route("/backup", methods=["POST"])
@admin_required
def backup():
    pl.create_backup()
    return redirect(url_for("dashboard", subtab="overall", flash="Backup created."))


@app.route("/restore-backup", methods=["POST"])
@admin_required
def restore_backup_route():
    restored = pl.restore_backup()
    flash = "Store restored from backup." if restored else "No backup exists yet."
    return redirect(url_for("dashboard", subtab="overall", flash=flash))


if __name__ == "__main__":
    # Local dev: python webapp.py, unchanged (port 5000, localhost only).
    # On a real host (Render etc.), gunicorn imports `app` directly instead
    # of this block ever running - see Procfile - and the platform supplies
    # PORT; host 0.0.0.0 here is just so this still works if anyone runs
    # this file directly on such a host instead of via gunicorn.
    app.run(debug=False, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
