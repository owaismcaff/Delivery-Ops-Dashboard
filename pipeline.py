"""
pipeline.py

Cleaning, merging, and metric-calculation logic for the FASR / RTO / NDR
daily dashboard. Kept separate from app.py so it can be tested or reused
outside Streamlit.

Data model
----------
The persistent store (data/raw_store.parquet) holds one row per Order code -
the same shape as the "Raw" sheet in the original workbook:

    Final Status2, Order date, Order code, channel_name,
    first_ofd_date, Attempt counts, Payment mode, Grouped_Courier_v2

plus State and Delivered_date (present in the regular raw export, absent
from the original workbook's seed - see OPTIONAL_COLUMNS below).

FASR / RTO / NDR / Flag and the newer derived metrics are NOT stored -
they're cheap to compute from the columns above, so they're derived fresh
every time the dashboard loads.

CANCELLED rows are kept in the store (not dropped during cleaning) - use the
Status filter in the sidebar to include/exclude them or any other status.

Merge behaviour
----------------
Each uploaded CSV is treated as "new/updated orders since the last upload".
Cleaning collapses duplicate Order codes within the file itself (SKU-line-
item level -> order level). The cleaned file is then upserted into the
store by Order code: if an order code already exists in the store (its
status changed - RTO resolved, attempt count went up), the newly uploaded
row replaces the old one; otherwise it's added as new.

A separate, append-only upload log (data/upload_log.csv) records every file
that's ever been merged in, so the dashboard can show a visible history of
what's been uploaded and prove the store isn't losing data between uploads
or app restarts.
"""

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

CORE_COLUMNS = [
    "Final Status2",
    "Order date",
    "Order code",
    "channel_name",
    "first_ofd_date",
    "Attempt counts",
    "Payment mode",
    "Grouped_Courier_v2",
]
# Present in the regular raw export but not in the original workbook's Raw
# sheet (and so absent from seed_original_raw.csv) - filled with a safe
# default when missing, rather than requiring every file to have them.
# "Key" (pickup pincode + destination pincode, underscore-joined) is used to
# derive the Pincode Cut (see _derive_pincode() below) AND retained directly
# as the Lane Cut's own column (see clean_upload() - it's dropped from the
# store afterward, replaced by the "Lane" column it was copied into).
OPTIONAL_COLUMNS = ["State", "Delivered_date", "Key"]
RAW_COLUMNS = CORE_COLUMNS + OPTIONAL_COLUMNS
# The actual processed/internal schema load_store() produces once a store
# exists (State/Delivered_date/Pincode/Lane/City/Brand - derived columns,
# not the raw "Key"/"Brand Name" input names). Used only for the empty-store
# fallback (no store file yet, e.g. right after Reset) so a fresh/empty
# dashboard has the same columns every downstream function expects, instead
# of crashing on a missing "Brand" (or, previously, a missing City/Lane/
# Pincode) the first time someone loads an empty store.
INTERNAL_COLUMNS = CORE_COLUMNS + ["State", "Delivered_date", "Pincode", "Lane", "City", "Brand"]

# Two raw export formats are accepted, auto-detected by the presence of
# "display_order_code" (the Final Base Table's own unique order id - not
# present in the GMV Table format at all). GMV Table is this dashboard's
# original format and needs no renaming - its raw column names already
# match RAW_COLUMNS above. The Final Base Table is a much wider (90+
# column), differently-named, one-row-per-order export (same one the
# lanewise_dashboard project's pipeline_core.py already knows how to read,
# for its own, different, Lane-based metrics) - only the columns below are
# pulled out of it and renamed to the same internal names GMV Table uses,
# so with_metrics() and every wide-table/Reports function downstream run
# completely unchanged regardless of which format was uploaded.
# first_ofd_date is spelled identically in both formats, so it needs no
# entry here.
FINAL_BASE_TABLE_COLUMN_MAP = {
    "display_order_code": "Order code",
    "order_date": "Order date",
    "Revised_status1": "Final Status2",
    "channel": "channel_name",
    "delivery_attempts": "Attempt counts",
    "payment_mode": "Payment mode",
    "primary_courier": "Grouped_Courier_v2",
    "shipping_state": "State",
    "delivered_date": "Delivered_date",
    "key": "Key",
}


# India's 28 states + 8 UTs, canonical spelling (chosen to match whichever
# variant was already most common in the data).
INDIA_STATES_UTS = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal",
    "Andaman & Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu & Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

# Raw exports mix full names, old names, ALL-CAPS/lower-case, and 2-letter
# codes for the same state - collapses everything down to the 36 canonical
# names above. Applied in load_store() (not clean_upload()) so it's
# self-healing: it normalizes both newly-uploaded rows AND whatever's
# already sitting in the store, on every load, with no one-time migration
# needed. "UT" (3 rows) is too ambiguous a code to confidently map to a
# specific state/UT, so it's left as Unknown rather than guessed.
STATE_NORMALIZE = {name.lower(): name for name in INDIA_STATES_UTS}
STATE_NORMALIZE.update({
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
    "jammu and kashmir": "Jammu & Kashmir",
    "andaman and nicobar islands": "Andaman & Nicobar Islands",
    "andaman & nicobar": "Andaman & Nicobar Islands",
    "andaman and nicobar": "Andaman & Nicobar Islands",
    "-": "Unknown",
    "ut": "Unknown",
    # 2-letter state/UT codes
    "ap": "Andhra Pradesh", "ar": "Arunachal Pradesh", "as": "Assam",
    "br": "Bihar", "cg": "Chhattisgarh", "ga": "Goa", "gj": "Gujarat",
    "hr": "Haryana", "hp": "Himachal Pradesh", "jh": "Jharkhand",
    "ka": "Karnataka", "kl": "Kerala", "mp": "Madhya Pradesh",
    "mh": "Maharashtra", "mn": "Manipur", "ml": "Meghalaya", "mz": "Mizoram",
    "nl": "Nagaland", "or": "Odisha", "pb": "Punjab", "rj": "Rajasthan",
    "sk": "Sikkim", "tn": "Tamil Nadu", "tl": "Telangana", "tg": "Telangana",
    "tr": "Tripura", "up": "Uttar Pradesh", "uk": "Uttarakhand",
    "wb": "West Bengal", "an": "Andaman & Nicobar Islands", "ch": "Chandigarh",
    "dl": "Delhi", "jk": "Jammu & Kashmir", "la": "Ladakh", "ld": "Lakshadweep",
    "py": "Puducherry", "dn": "Dadra and Nagar Haveli and Daman and Diu",
})


def _normalize_state(series: pd.Series) -> pd.Series:
    key = series.astype(str).str.strip().str.lower()
    mapped = key.map(STATE_NORMALIZE)
    # Anything not in the explicit map above (an entirely new/unanticipated
    # raw value) is title-cased as a last-resort fallback, so it still
    # collapses consistently rather than being dropped or left as "Unknown".
    return mapped.fillna(series.astype(str).str.strip().str.title())


def _derive_pincode(df: pd.DataFrame) -> pd.Series:
    """Destination pincode, split out of the Lane string (Key = pickup
    pincode + destination pincode, underscore-joined) - the second half is
    the destination pincode, validated as an exact match to the raw export's
    own destination-pincode field with 0 mismatches (see lanewise_dashboard's
    pipeline_core.py, which relies on the same Key field for its own Lane
    metrics). Works identically for both raw schemas, since Key/key is
    present in both. "Unknown" where Key is missing or malformed."""
    key = df["Key"].astype(str) if "Key" in df.columns else pd.Series("", index=df.index)
    pincode = key.str.split("_").str[1]
    return pincode.fillna("Unknown").replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})


CITY_MASTER_PATH = Path(__file__).parent / "data_ref" / "pincode_master.csv"
_city_master_cache = {"map": None}


def _load_pincode_city_map() -> dict:
    if _city_master_cache["map"] is None:
        if not CITY_MASTER_PATH.exists():
            _city_master_cache["map"] = {}
        else:
            master = pd.read_csv(CITY_MASTER_PATH, dtype=str, usecols=["Post Code", "City"])
            master["Post Code"] = master["Post Code"].str.strip().str.zfill(6)
            master["City"] = master["City"].str.strip().str.title()
            master = master.drop_duplicates(subset=["Post Code"], keep="first")
            _city_master_cache["map"] = dict(zip(master["Post Code"], master["City"]))
    return _city_master_cache["map"]


def _derive_city(pincode_series: pd.Series) -> pd.Series:
    """City looked up from the destination Pincode via data_ref/pincode_master.csv
    (~31k pincode -> city mappings), not read from the raw export directly -
    so it's available uniformly for every row regardless of which raw format
    was uploaded (GMV Table exports have no city field at all) and for every
    order already in the store, not just newly-uploaded files. "Unknown"
    where the pincode has no match in the master (or is itself "Unknown")."""
    city_map = _load_pincode_city_map()
    mapped = pincode_series.astype(str).str.strip().str.zfill(6).map(city_map)
    return mapped.fillna("Unknown")


# Brand is derived from channel_name (a CORE_COLUMNS field, always present
# in every row regardless of upload format or how old the order is), per
# the user's own channel->brand classification - NOT from the raw export's
# "Brand Name" column (GMV Table only, missing from Final Base Table, and
# would've required re-uploading every historical file to backfill) and
# NOT from Order code prefixes (confirmed unreliable: a "Fien"-prefixed
# order's real Brand Name was "Mcaffeine", not a separate "Fein" brand).
# Keys compared case-insensitively since raw channel_name casing is
# inconsistent across uploads (e.g. "Marketing" vs "COMPENSATION").
_CHANNEL_BRAND_GROUPS = {
    "Mcaffeine": ["MCAFFEINE_D2C", "SHOPIFY", "FIEN_SHOPIFY", "HYPD", "COMPENSATION", "MARKETING", "MCAF_SHOPIFY.IN"],
    "Hyphen": ["HYP_SHOPIFY", "HYPD_HYPHEN", "HYPHEN_D2C", "HYP_MARKETING", "HYP_COMPENSATION", "HYP_SHOPIFY_IN"],
    "DND": ["DND_SHOPIFY", "DND_D2C", "DND_MARKETING", "DND_COMPENSATION"],
}
CHANNEL_TO_BRAND = {channel: brand for brand, channels in _CHANNEL_BRAND_GROUPS.items() for channel in channels}


def _derive_brand(channel_series: pd.Series) -> pd.Series:
    """Brand looked up from channel_name via CHANNEL_TO_BRAND - "Unknown"
    for any channel_name not in the mapping (new/unseen channel values)."""
    upper = channel_series.astype(str).str.strip().str.upper()
    return upper.map(CHANNEL_TO_BRAND).fillna("Unknown")


def _derive_lane_from_legacy_key(key_series: pd.Series) -> pd.Series:
    """Backward-compat only, for stores built before the Lane Cut existed:
    an even older version of the pipeline never dropped the raw "Key"
    column at all, so a lot of historical rows already carry real lane
    data under that name - worth recovering rather than showing a blanket
    "Unknown" for every pre-existing order. Handles both separators
    actually found in this column across different historical upload
    batches - "_" (the current, correct raw-export format) and "-" (an
    older batch's format, same pickup_dest shape, just a different join
    character) - normalizing to "_" either way. A bare value with neither
    separator (no lane info at all, ~4% of this store) or a missing Key
    falls back to "Unknown", same as any other unresolvable case."""
    key = key_series.astype(str).str.strip().str.replace("-", "_", regex=False)
    has_lane_shape = key.str.contains("_", regex=False)
    return key.where(has_lane_shape, "Unknown").replace({"nan": "Unknown", "None": "Unknown", "": "Unknown"})


DATE_FORMAT_IN = "%d/%m/%Y %H:%M:%S"  # e.g. 01/07/2026 00:00:00

PAYMENT_GROUPS = ["COD", "PPD"]

# (row label, numerator column, denominator column). Most are "of all
# orders"; the NDR-to-X metrics are "of orders that were NDR on the 1st
# attempt" (denominator = NDR, not Flag). This is the ORIGINAL dashboard's
# metric set - unchanged. The attempt-cascade breakdown lives separately
# in ATTEMPT_METRICS below, on its own tab, to avoid cluttering this one.
METRICS = [
    ("FASR %", "FASR", "Flag"),
    ("RTO in 1st Att %", "RTO", "Flag"),
    ("RTO % (Overall)", "RTO_OVERALL", "Flag"),
    ("NDR %", "NDR", "Flag"),
    ("NDR to Delivery %", "NDR_TO_DELIVERY", "NDR"),
    ("NDR to RTO %", "NDR_TO_RTO", "NDR"),
    ("NDR to Pending %", "NDR_TO_PENDING", "NDR"),
]
# "Orders" isn't a ratio (no num/denom pair), so it isn't in METRICS itself,
# but it's still a selectable row in the 1D/2D Cut tabs' metric checklist -
# this is the full list of what a user can check, in display order.
SELECTABLE_METRIC_LABELS = [label for label, _, _ in METRICS] + ["Orders"]

# Attempt-cascade breakdown (separate "Attempt Analysis" tab), matching the
# reference sheet's structure: every stage uses the SAME denominator (total
# orders in scope) - not a shrinking sub-population - so these percentages
# are slices of one 100% pie and sum straight across: Delivered % (Overall)
# = FASR + 2nd + 3rd + 3+ Attempt Delivered %, same for RTO % (Overall).
# The "Pending %" rows (added on top of the reference sheet, so every stage
# hits exactly 100%: Delivered% + RTO% + Pending% = that stage's own
# population) use a narrower definition than "NDR %" below - "still stuck
# exactly at this attempt," not "didn't resolve on the 1st attempt" (which
# also counts orders that moved on and resolved later).
ATTEMPT_METRICS = [
    ("1st Attempt: FASR %", "FASR", "Flag"),
    ("1st Attempt: RTO %", "RTO", "Flag"),
    ("1st Attempt: Pending %", "PENDING_1ST", "Flag"),
    ("2nd Attempt: Delivered %", "DELIVERED_2ND", "Flag"),
    ("2nd Attempt: RTO %", "RTO_2ND", "Flag"),
    ("2nd Attempt: Pending %", "NDR_2ND", "Flag"),
    ("3rd Attempt: Delivered %", "DELIVERED_3RD", "Flag"),
    ("3rd Attempt: RTO %", "RTO_3RD", "Flag"),
    ("3rd Attempt: Pending %", "NDR_3RD", "Flag"),
    ("3+ Attempts: Delivered %", "DELIVERED_3PLUS", "Flag"),
    ("3+ Attempts: RTO %", "RTO_3PLUS", "Flag"),
    ("3+ Attempts: Pending %", "NDR_3PLUS", "Flag"),
    ("Delivered % (Overall)", "DELIVERED_OVERALL", "Flag"),
    ("RTO % (Overall)", "RTO_OVERALL", "Flag"),
    ("NDR %", "NDR", "Flag"),
    ("NDR to Delivery %", "NDR_TO_DELIVERY", "NDR"),
]

# Local dev: a "data" folder next to the code, as always. On a host with a
# persistent disk (e.g. Render) that disk is mounted at its own path, not
# necessarily next to the code - FASR_DATA_DIR overrides it for that case.
DATA_DIR = Path(os.environ.get("FASR_DATA_DIR") or (Path(__file__).parent / "data"))
STORE_PATH = DATA_DIR / "raw_store.parquet"
UPLOAD_LOG_PATH = DATA_DIR / "upload_log.csv"

# Jan-Jun 2026 date-bucketing note (2026-09-21): that range was a one-time
# exercise computed on Order date instead of first_ofd_date, then hardcoded
# by overwriting first_ofd_date = Order date for those rows directly in the
# persisted store (see the recovery script that did this) - so every metric
# below, which all bucket on first_ofd_date, naturally reflects Order date
# for Jan-Jun without any special-casing here. Everything from Jul 2026
# onward uses real first_ofd_date. This replaces a briefly-used rolling
# dual-date "bucket_date" system, reverted the same day per explicit
# request - back to plain first_ofd_date going forward.


# ---- Cleaning ---------------------------------------------------------------

def _parse_dates(series: pd.Series) -> pd.Series:
    """Parse a date column, trying the primary export's format first
    (fast) and falling back to a flexible parse if that format doesn't
    match most of the values - so a differently-formatted file (like a
    seed export pulled from a different source) doesn't silently turn
    into all-null dates."""
    parsed = pd.to_datetime(series, format=DATE_FORMAT_IN, errors="coerce")
    non_null = series.notna().sum()
    if non_null > 0 and parsed.notna().sum() < 0.5 * non_null:
        parsed = pd.to_datetime(series, dayfirst=True, errors="coerce")
    return parsed


def clean_upload(raw_csv_path_or_buffer) -> tuple[pd.DataFrame, dict]:
    """Read + clean one uploaded raw export - either the GMV Table format or
    the Final Base Table format (auto-detected, see FINAL_BASE_TABLE_COLUMN_MAP).
    Returns (cleaned_df, stats). CANCELLED rows are kept (filter them via the
    Status sidebar filter, not here) - only duplicate Order codes are collapsed."""
    if hasattr(raw_csv_path_or_buffer, "seek"):
        raw_csv_path_or_buffer.seek(0)
    header_cols = set(pd.read_csv(raw_csv_path_or_buffer, nrows=0).columns)

    is_final_base_table = "display_order_code" in header_cols
    rename_map = FINAL_BASE_TABLE_COLUMN_MAP if is_final_base_table else {}
    reverse_map = {internal: raw for raw, internal in rename_map.items()}

    if hasattr(raw_csv_path_or_buffer, "seek"):
        raw_csv_path_or_buffer.seek(0)  # defensive: Streamlit file objects are re-read across reruns
    cols_to_read = [reverse_map.get(c, c) for c in RAW_COLUMNS if reverse_map.get(c, c) in header_cols]

    df = pd.read_csv(raw_csv_path_or_buffer, usecols=cols_to_read, dtype=str)
    if rename_map:
        df = df.rename(columns=rename_map)
    n_raw = len(df)

    if "State" not in df.columns:
        df["State"] = "Unknown"
    df["State"] = df["State"].fillna("Unknown")
    if "Delivered_date" not in df.columns:
        df["Delivered_date"] = None
    # Brand is derived later, in load_store() (see _derive_brand) - from
    # channel_name, which every row here already has (a CORE_COLUMNS field),
    # not from a raw column read here.

    df["Pincode"] = _derive_pincode(df)
    # Lane Cut's own column - the raw Key string itself (already exactly
    # "pickup_pincode_destination_pincode"), not re-derived like Pincode is.
    if "Key" in df.columns:
        df["Lane"] = df["Key"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
    else:
        df["Lane"] = "Unknown"
    df = df.drop(columns=["Key"], errors="ignore")

    df = df.drop_duplicates(subset=["Order code"], keep="first").copy()
    n_after_dedup = len(df)

    df["Order date"] = _parse_dates(df["Order date"])
    df["first_ofd_date"] = _parse_dates(df["first_ofd_date"]).dt.normalize()
    df["Delivered_date"] = _parse_dates(df["Delivered_date"]).dt.normalize()
    df["Attempt counts"] = pd.to_numeric(df["Attempt counts"], errors="coerce").fillna(0).astype(int)

    stats = {
        "format": "Final Base Table" if is_final_base_table else "GMV Table",
        "rows_in_file": n_raw,
        "after_removing_duplicate_orders": n_after_dedup,
    }
    return df.reset_index(drop=True), stats


# ---- Store (persistent, on disk) --------------------------------------------

def _fix_string_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """pd.read_parquet defaults string columns to a PyArrow-backed dtype,
    which is dramatically slower for the repeated boolean-mask filtering
    this app does everywhere (verified: ~800ms per filter vs ~10ms after
    this conversion, on the full store). Convert once, here, rather than
    paying that cost on every filter downstream. Shared by load_store() and
    load_cube_cache() since both read parquet files back off disk."""
    str_cols = df.select_dtypes(include=["string", "str"]).columns
    for col in str_cols:
        df[col] = df[col].astype(object)
    return df


def load_store() -> pd.DataFrame:
    if STORE_PATH.exists():
        df = _fix_string_dtypes(pd.read_parquet(STORE_PATH))
        if "State" not in df.columns:
            df["State"] = "Unknown"
        df["State"] = _normalize_state(df["State"])
        if "Delivered_date" not in df.columns:
            df["Delivered_date"] = pd.NaT
        if "Pincode" not in df.columns:
            df["Pincode"] = "Unknown"
        if "Lane" not in df.columns:
            if "Key" in df.columns:
                # An even older version of the pipeline never dropped Key at
                # all - recover real Lane data from it rather than showing a
                # blanket "Unknown" for every pre-existing order.
                df["Lane"] = _derive_lane_from_legacy_key(df["Key"])
            else:
                df["Lane"] = "Unknown"
        # Key only ever existed for this one-time backward-compat recovery -
        # dropping it here (from the in-memory frame load_store() returns,
        # not just the derived Lane column) means the next upload's merge +
        # save_store() naturally stops persisting it at all, rather than it
        # lingering forever as a dead, all-NaN-for-new-rows column.
        df = df.drop(columns=["Key"], errors="ignore")
        # City: prefer an explicit value already in the store (the live-DB
        # path stores dest_district directly, per explicit choice - same
        # as State) over the pincode-master lookup. GMV/Final Base Table
        # rows never have a raw City field at all, so they fall through to
        # the derived value unconditionally; DB-sourced rows only fall
        # through when dest_district itself was null for that order.
        derived_city = _derive_city(df["Pincode"])
        if "City" in df.columns:
            df["City"] = df["City"].where(df["City"].notna(), derived_city)
        else:
            df["City"] = derived_city
        # Brand: always (re)derived from channel_name, not read/preserved
        # from any prior "Brand"/"Brand Name" value - this is the single
        # source of truth (see _derive_brand), so it's consistent for every
        # row regardless of upload format or how it got into the store, and
        # auto-corrects itself if CHANNEL_TO_BRAND is ever updated later.
        df["Brand"] = _derive_brand(df["channel_name"])
        return df
    return pd.DataFrame(columns=INTERNAL_COLUMNS)


def save_store(df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(STORE_PATH, index=False)


# Columns where "Unknown" (or null) means "this particular upload didn't
# carry this data" rather than "this order genuinely has no X" - e.g. a
# Final Base Table upload has no pickup/destination-pincode field at all,
# so clean_upload() sets Lane/Pincode to "Unknown" for every row in it, and
# some uploads leave State blank. Without PRESERVE_IF_UNKNOWN_COLUMNS, a
# plain "last upload wins" upsert would silently overwrite a previously-good
# value with "Unknown" the moment any such narrower file touched that order
# again - confirmed as the actual cause of a real production incident: a
# Final Base Table upload on 2026-09-21 wiped Lane back to "Unknown" for
# ~65k orders it updated, and only Jun/Jul/Aug (not Sep) ever got a
# follow-up GMV Table re-upload that happened to repair it as a side effect.
# City isn't listed here - it's never persisted directly (load_store()
# recomputes it fresh from Pincode every load), so preserving Pincode here
# already fixes City transitively on the next load.
PRESERVE_IF_UNKNOWN_COLUMNS = ["State", "Pincode", "Lane"]


def merge_into_store(new_df: pd.DataFrame) -> dict:
    """Upsert new_df into the persistent store, keyed on Order code."""
    store = load_store()
    n_before = len(store)
    existing_codes = set(store["Order code"]) if n_before else set()
    n_new_orders = int((~new_df["Order code"].isin(existing_codes)).sum())
    n_updated_orders = len(new_df) - n_new_orders

    if n_before:
        new_df = new_df.copy()
        existing_lookup = store.drop_duplicates(subset=["Order code"], keep="last").set_index("Order code")
        for col in PRESERVE_IF_UNKNOWN_COLUMNS:
            if col not in new_df.columns or col not in existing_lookup.columns:
                continue
            prev_vals = new_df["Order code"].map(existing_lookup[col])
            new_is_unknown = new_df[col].isna() | (new_df[col] == "Unknown")
            prev_is_good = prev_vals.notna() & (prev_vals != "Unknown")
            fill_mask = new_is_unknown & prev_is_good
            new_df.loc[fill_mask, col] = prev_vals[fill_mask]

    combined = pd.concat([store, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Order code"], keep="last").reset_index(drop=True)

    save_store(combined)
    return {
        "orders_before_merge": n_before,
        "new_orders_added": n_new_orders,
        "existing_orders_updated": n_updated_orders,
        "orders_after_merge": len(combined),
    }


def reset_store() -> None:
    if STORE_PATH.exists():
        STORE_PATH.unlink()
    if UPLOAD_LOG_PATH.exists():
        UPLOAD_LOG_PATH.unlink()
    for p in (CUBE_CACHE_PATH, CITY_CUBE_CACHE_PATH, PINCODE_CUBE_CACHE_PATH, LANE_CUBE_CACHE_PATH, CUBE_CACHE_META_PATH):
        if p.exists():
            p.unlink()


# ---- Manual backup (single slot, overwritten each time) ---------------------
# Not automatic - triggered only by the "Create Backup" sidebar button, so a
# backup only exists when someone deliberately made one right before a risky
# action (like Reset). Each new backup replaces the previous one; this is a
# single safety-net snapshot, not a version history.

BACKUP_STORE_PATH = DATA_DIR / "raw_store_backup.parquet"
BACKUP_UPLOAD_LOG_PATH = DATA_DIR / "upload_log_backup.csv"


def create_backup() -> None:
    if STORE_PATH.exists():
        shutil.copyfile(STORE_PATH, BACKUP_STORE_PATH)
    if UPLOAD_LOG_PATH.exists():
        shutil.copyfile(UPLOAD_LOG_PATH, BACKUP_UPLOAD_LOG_PATH)


def restore_backup() -> bool:
    """Restores the store from the single backup slot, if one exists.
    Returns False (no-op) if no backup has ever been created."""
    if not BACKUP_STORE_PATH.exists():
        return False
    shutil.copyfile(BACKUP_STORE_PATH, STORE_PATH)
    if BACKUP_UPLOAD_LOG_PATH.exists():
        shutil.copyfile(BACKUP_UPLOAD_LOG_PATH, UPLOAD_LOG_PATH)
    return True


def backup_timestamp() -> str | None:
    if not BACKUP_STORE_PATH.exists():
        return None
    return datetime.fromtimestamp(BACKUP_STORE_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


# ---- Upload log (persistent, append-only, on disk) --------------------------

UPLOAD_LOG_COLUMNS = [
    "uploaded_at", "filename", "format", "rows_in_file",
    "after_removing_duplicate_orders", "new_orders_added",
    "existing_orders_updated", "orders_after_merge",
]


def load_upload_log() -> pd.DataFrame:
    if UPLOAD_LOG_PATH.exists():
        return pd.read_csv(UPLOAD_LOG_PATH)
    return pd.DataFrame(columns=UPLOAD_LOG_COLUMNS)


def append_upload_log(filename: str, clean_stats: dict, merge_stats: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "filename": filename,
        **clean_stats,
        **{k: v for k, v in merge_stats.items() if k != "orders_before_merge"},
    }
    log = load_upload_log()
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    log.to_csv(UPLOAD_LOG_PATH, index=False)


# ---- Derived metrics --------------------------------------------------------

def with_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["FASR"] = ((df["Attempt counts"] == 1) & (df["Final Status2"] == "Delivered")).astype(int)
    df["RTO"] = ((df["Attempt counts"] == 1) & (df["Final Status2"] == "RTO")).astype(int)
    df["NDR"] = 1 - df["RTO"] - df["FASR"]
    df["Flag"] = 1

    # RTO overall: Final Status2 == RTO regardless of attempt count.
    df["RTO_OVERALL"] = (df["Final Status2"] == "RTO").astype(int)

    # Of orders that were NDR on the 1st attempt, what eventually happened:
    # delivered late, RTO'd late, or still pending/unresolved.
    is_ndr = df["NDR"] == 1
    df["NDR_TO_DELIVERY"] = (is_ndr & (df["Final Status2"] == "Delivered")).astype(int)
    df["NDR_TO_RTO"] = (is_ndr & (df["Final Status2"] == "RTO")).astype(int)
    df["NDR_TO_PENDING"] = (is_ndr & ~df["Final Status2"].isin(["Delivered", "RTO"])).astype(int)

    # Attempt cascade: 2nd/3rd/3+ Delivered% and RTO% all use the SAME
    # denominator as 1st Attempt (total orders in scope) - not a shrinking
    # sub-population - so every stage is a slice of the same 100% pie and
    # they can be summed straight across (Final Delivered% / Final RTO%
    # below). "3+" = more than 3 attempts, kept as one combined bucket.
    attempt = df["Attempt counts"]
    status = df["Final Status2"]
    not_terminal = ~status.isin(["Delivered", "RTO"])

    # Narrow "still stuck exactly here" flag, distinct from the broad
    # existing NDR (1-FASR-RTO, which also counts orders that already moved
    # on to and resolved at a later attempt). Used for the per-stage
    # "Pending %" rows so Delivered% + RTO% + Pending% = 100% of that
    # stage's own population, cleanly, for every attempt stage including 1st.
    df["PENDING_1ST"] = ((attempt == 1) & not_terminal).astype(int)

    df["DELIVERED_2ND"] = ((attempt == 2) & (status == "Delivered")).astype(int)
    df["RTO_2ND"] = ((attempt == 2) & (status == "RTO")).astype(int)
    df["NDR_2ND"] = ((attempt == 2) & not_terminal).astype(int)

    df["DELIVERED_3RD"] = ((attempt == 3) & (status == "Delivered")).astype(int)
    df["RTO_3RD"] = ((attempt == 3) & (status == "RTO")).astype(int)
    df["NDR_3RD"] = ((attempt == 3) & not_terminal).astype(int)

    df["DELIVERED_3PLUS"] = ((attempt > 3) & (status == "Delivered")).astype(int)
    df["RTO_3PLUS"] = ((attempt > 3) & (status == "RTO")).astype(int)
    df["NDR_3PLUS"] = ((attempt > 3) & not_terminal).astype(int)

    # Overall delivery rate regardless of attempt count (pairs with the
    # existing RTO_OVERALL) - sums to FASR + Delivered_2nd + Delivered_3rd
    # + Delivered_3+ automatically, since Final Status2 == "Delivered" can
    # only happen at exactly one attempt count per order.
    df["DELIVERED_OVERALL"] = (status == "Delivered").astype(int)

    return df


# ---- Pre-aggregated cube (perf) ----------------------------------------------
# Every metric in this app is a SUM (or a ratio of two sums) over the flag
# columns with_metrics() creates - so grouping the full order-level store
# down to one row per (date, channel, payment mode, state, status, courier)
# combination, with every flag pre-summed, is mathematically identical to
# summing the raw per-order rows directly - just over a far smaller table
# (real combinations actually occurring are a small fraction of the
# theoretical cross-product, and nowhere near the multi-million order
# count). Filtering + re-summing the cube gives the same numbers as
# filtering + summing the raw rows, for any filter combination, since sum
# is associative over the collapsed dimensions.
#
# Pincode/City are deliberately EXCLUDED from the MAIN cube - their
# cardinality means grouping by them barely shrinks the table at all, so
# it would defeat the point. Each gets its own dedicated cube instead (see
# build_city_cube / build_pincode_cube below), with the DISPLAYED table
# bounded via the min-orders filter (discover_groups_above_threshold)
# rather than by excluding them from aggregation entirely.
CUBE_DIMENSIONS = ["first_ofd_date", "channel_name", "Brand", "Payment mode", "State", "Final Status2", "Grouped_Courier_v2"]
CUBE_SUM_COLS = [
    "Flag", "FASR", "RTO", "RTO_OVERALL", "NDR", "NDR_TO_DELIVERY", "NDR_TO_RTO", "NDR_TO_PENDING",
    "PENDING_1ST", "DELIVERED_2ND", "RTO_2ND", "NDR_2ND",
    "DELIVERED_3RD", "RTO_3RD", "NDR_3RD",
    "DELIVERED_3PLUS", "RTO_3PLUS", "NDR_3PLUS", "DELIVERED_OVERALL",
]


def _to_category(cube: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Converts a cube's low-cardinality dimension columns (State, Courier,
    City, Pincode, etc - NOT first_ofd_date, which stays a real datetime)
    to pandas category dtype, in place. After groupby+reset_index, each of
    these columns repeats a small set of distinct values across every cube
    row - as plain object dtype that's a full Python string object per
    row; as category dtype it's a small integer code plus ONE shared copy
    of each distinct string. No behavior change: every groupby on these
    cubes already uses observed=True (categorical-safe), and .isin()/
    comparisons/to_parquet all work transparently on categoricals. Verified
    on the Pincode cube (the largest, ~2.9M rows): 742MB -> 489MB (-34%)."""
    for c in cols:
        if c in cube.columns:
            cube[c] = cube[c].astype("category")
    return cube


def build_cube(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse a with_metrics()-applied order-level dataframe into the
    pre-aggregated cube described above. Rows with no first_ofd_date are
    dropped here (every wide-table/aggregate function already excludes them
    via its own dropna, so this loses nothing downstream)."""
    d = df.dropna(subset=["first_ofd_date"]).copy()
    # dropna=False: pandas' groupby silently DROPS rows where any group-key
    # column is NaN by default - a handful of orders have NaN in State/
    # Final Status2/Grouped_Courier_v2 even with a real first_ofd_date, and
    # without this they'd silently vanish from every cube-based number
    # (verified: this exact bug undercounted total Orders by ~29k before
    # this flag was added).
    cube = d.groupby(CUBE_DIMENSIONS, dropna=False, observed=True)[CUBE_SUM_COLS].sum().reset_index()
    return _to_category(cube, CUBE_DIMENSIONS[1:])


# City Cut / City x Courier Cut / State x Courier Cut / the unified 1D Cut
# tab all need City or Pincode as an extra dimension - neither is one of
# the main cube's 6, so each gets its own cube, carrying the same
# CUBE_SUM_COLS as the main cube (not just Flag/FASR - the 1D Cut tab lets
# the user pick ANY metric for ANY dimension, not just FASR%). Built
# alongside the main cube, from the same transient full_df pass, so
# there's no second expensive reload. Pincode's cardinality (tens of
# thousands) is far higher than City's (a few thousand) - nothing here
# caps it; the min-orders filter (discover_groups_above_threshold) is what
# keeps the DISPLAYED table from showing statistically meaningless
# single-order pincodes, same mechanism that made City safe.
CITY_CUBE_DIMENSIONS = CUBE_DIMENSIONS + ["City"]
PINCODE_CUBE_DIMENSIONS = CUBE_DIMENSIONS + ["Pincode"]
# Shared default for the 1D/2D Cut tabs' min-orders field, regardless of
# which dimension is actually picked.
CITY_CUT_DEFAULT_MIN_ORDERS = 0


def build_city_cube(df: pd.DataFrame) -> pd.DataFrame:
    """Cube adding City as a 7th dimension on top of the main cube's 6.
    City is derived from Pincode via pincode_master.csv (see
    _derive_city), not read from the raw export, so it's populated for
    every order regardless of upload format or how old the data is."""
    d = df.dropna(subset=["first_ofd_date"]).copy()
    cube = d.groupby(CITY_CUBE_DIMENSIONS, dropna=False, observed=True)[CUBE_SUM_COLS].sum().reset_index()
    return _to_category(cube, CITY_CUBE_DIMENSIONS[1:])


def build_pincode_cube(df: pd.DataFrame) -> pd.DataFrame:
    """Cube adding Pincode as a 7th dimension - same idea as
    build_city_cube, at much higher cardinality."""
    d = df.dropna(subset=["first_ofd_date"]).copy()
    cube = d.groupby(PINCODE_CUBE_DIMENSIONS, dropna=False, observed=True)[CUBE_SUM_COLS].sum().reset_index()
    return _to_category(cube, PINCODE_CUBE_DIMENSIONS[1:])


LANE_CUBE_DIMENSIONS = CUBE_DIMENSIONS + ["Lane"]


def build_lane_cube(df: pd.DataFrame) -> pd.DataFrame:
    """Cube adding Lane (pickup_pincode_destination_pincode, see
    clean_upload's retained "Key" column) as a 7th dimension - same idea as
    build_pincode_cube, at comparable-or-higher cardinality (a Lane is a
    Pincode further split by pickup location)."""
    d = df.dropna(subset=["first_ofd_date"]).copy()
    cube = d.groupby(LANE_CUBE_DIMENSIONS, dropna=False, observed=True)[CUBE_SUM_COLS].sum().reset_index()
    return _to_category(cube, LANE_CUBE_DIMENSIONS[1:])


# ---- Cube disk-cache (perf) --------------------------------------------------
# Building all 3 cubes means grouping the full 5.3M-row store 3 times (the
# Pincode cube's groupby alone, at tens of thousands of distinct Pincodes,
# is genuinely slow) - unavoidable the FIRST time data actually changes, but
# wasteful to redo on every plain server restart during testing, when the
# store file itself hasn't changed at all since the cubes were last built.
# Cached here as 3 small parquet files + a sidecar recording which store
# mtime they were built from; get_cube() in webapp.py loads these instead
# of rebuilding whenever that mtime still matches.
CUBE_CACHE_PATH = DATA_DIR / "cube_cache.parquet"
CITY_CUBE_CACHE_PATH = DATA_DIR / "city_cube_cache.parquet"
PINCODE_CUBE_CACHE_PATH = DATA_DIR / "pincode_cube_cache.parquet"
LANE_CUBE_CACHE_PATH = DATA_DIR / "lane_cube_cache.parquet"
CUBE_CACHE_META_PATH = DATA_DIR / "cube_cache_meta.txt"
# Bump whenever a cube's schema or dtypes change (e.g. the category-dtype
# memory optimization below, or adding the Lane cube as a 4th file) - an
# old on-disk cache built under a lower version is treated as a miss and
# rebuilt fresh, rather than silently loading back cubes in the old
# format (or, for version 2->3, trying to unpack a 4th file that was
# never written).
CUBE_CACHE_FORMAT_VERSION = 6


def save_cube_cache(
    cube: pd.DataFrame, city_cube: pd.DataFrame, pincode_cube: pd.DataFrame, lane_cube: pd.DataFrame,
    store_mtime: float,
) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cube.to_parquet(CUBE_CACHE_PATH, index=False)
    city_cube.to_parquet(CITY_CUBE_CACHE_PATH, index=False)
    pincode_cube.to_parquet(PINCODE_CUBE_CACHE_PATH, index=False)
    lane_cube.to_parquet(LANE_CUBE_CACHE_PATH, index=False)
    CUBE_CACHE_META_PATH.write_text(repr(store_mtime) + "|" + str(CUBE_CACHE_FORMAT_VERSION))


def load_cube_cache(store_mtime: float):
    """Returns (cube, city_cube, pincode_cube, lane_cube) read straight off
    disk if a cache exists, matches this exact store mtime, AND was built
    under the current CUBE_CACHE_FORMAT_VERSION, else None (a stale/
    missing/corrupt/outdated-format cache always falls back to a full
    rebuild, never raises)."""
    paths = [CUBE_CACHE_PATH, CITY_CUBE_CACHE_PATH, PINCODE_CUBE_CACHE_PATH, LANE_CUBE_CACHE_PATH, CUBE_CACHE_META_PATH]
    if not all(p.exists() for p in paths):
        return None
    try:
        meta = CUBE_CACHE_META_PATH.read_text()
        mtime_part, _, version_part = meta.partition("|")
        cached_mtime = float(mtime_part)
        cached_version = int(version_part) if version_part else 1
        if cached_mtime != store_mtime or cached_version != CUBE_CACHE_FORMAT_VERSION:
            return None
        return (
            _fix_string_dtypes(pd.read_parquet(CUBE_CACHE_PATH)),
            _fix_string_dtypes(pd.read_parquet(CITY_CUBE_CACHE_PATH)),
            _fix_string_dtypes(pd.read_parquet(PINCODE_CUBE_CACHE_PATH)),
            _fix_string_dtypes(pd.read_parquet(LANE_CUBE_CACHE_PATH)),
        )
    except (ValueError, OSError):
        return None


# Registry for the unified 1D Cut / 2D Cut tabs: every dimension a user can
# pick, its cube column, and which cube it lives in. "main" = the 6
# dimensions already in the primary cube (free - no extra memory, no extra
# build); "city"/"pincode" = their own dedicated cubes (see build_city_cube
# / build_pincode_cube above). Order here is the order dimensions appear in
# the picker dropdown.
DIMENSIONS = {
    "brand": {"label": "Brand", "column": "Brand", "source": "main"},
    "state": {"label": "State", "column": "State", "source": "main"},
    "payment": {"label": "Payment Mode", "column": "Payment mode", "source": "main"},
    "courier": {"label": "Courier", "column": "Grouped_Courier_v2", "source": "main"},
    "channel": {"label": "Channel", "column": "channel_name", "source": "main"},
    "city": {"label": "City", "column": "City", "source": "city"},
    "pincode": {"label": "Pincode", "column": "Pincode", "source": "pincode"},
    "lane": {"label": "Lane", "column": "Lane", "source": "lane"},
}

# City/Pincode/Lane are all "geography" dimensions derived from the same
# underlying pickup/destination pincode data, each a further split of the
# one before it (Pincode implies City; Lane - pickup pincode + destination
# pincode - implies Pincode) - pairing any two of them in the 2D Cut isn't
# a meaningful cut (see _resolve_2d_dimensions in webapp.py) and no cube
# carries more than one of them anyway.
GEO_DIMENSION_SOURCES = {"city", "pincode", "lane"}

# City/Pincode/Lane can have thousands to hundreds of thousands of distinct
# values - at min_orders=0, ranking/formatting/rendering literally all of
# them is real, unavoidable per-request work no amount of caching removes
# (this is also how every BI tool - Tableau, Looker, Power BI - actually
# handles high-cardinality drill-downs: they cap/paginate the interactive
# view rather than render every row). Applied only to City/Pincode/Lane
# cuts in the dashboard/attempt-analysis routes (see webapp.py) -
# State/Payment/Courier/Channel never need it, and CSV export deliberately
# stays uncapped since "download" should mean the complete data.
ROW_CAP_FOR_HIGH_CARDINALITY = 300


def warm_up_cube(cube: pd.DataFrame, extra_col: str | None = None) -> None:
    """Forces pandas to build its internal per-column grouping/hash
    structures right now, for every dimension this cube will ever actually
    get grouped by - pandas builds these lazily on first use per column,
    so without this, that one-time cost (genuinely slow for City/Pincode's
    1-3M rows) silently lands on whichever request happens to be the FIRST
    to pick that dimension in the 1D/2D Cut tabs, instead of on cube
    construction, which is already expected to take a moment. Called once
    right after building each cube; the throwaway results are discarded.
    extra_col is the cube's own extra dimension (City/Pincode) beyond the
    main 6 - also warmed up paired with Courier, since "City x Courier" /
    "Pincode x Courier" is the most likely 2D Cut combination."""
    dim_cols = ["channel_name", "Brand", "Payment mode", "State", "Final Status2", "Grouped_Courier_v2"]
    if extra_col:
        dim_cols.append(extra_col)
    for col in dim_cols:
        cube.groupby(col, observed=True)["Flag"].sum()
    if extra_col:
        cube.groupby([extra_col, "Grouped_Courier_v2"], observed=True)["Flag"].sum()


def _search_mask(names: pd.Index, search_terms: list[str]) -> pd.Series:
    """Case-insensitive "contains any of these terms" mask over an index of
    group names - the search escape hatch around ROW_CAP_FOR_HIGH_CARDINALITY:
    someone looking for one specific Pincode/City shouldn't have to hope
    it's in the top 300 by volume. Comma-separated terms (e.g. multiple
    pincodes to compare side by side) are OR'd together."""
    terms = [re.escape(t.strip().lower()) for t in search_terms if t.strip()]
    if not terms:
        return pd.Series(False, index=names)
    pattern = "|".join(terms)
    return pd.Series(names.astype(str).str.lower(), index=names).str.contains(pattern, regex=True, na=False)


def discover_groups_above_threshold(
    cube: pd.DataFrame, group_cols, min_orders: int = 0, search_terms: list[str] | None = None,
) -> list:
    """Distinct value(s) of group_cols whose total Flag sum in `cube` is at
    least min_orders, sorted by volume descending (used by City Cut - the
    two x Courier cuts apply this filtering internally instead, see
    discover_grouped_rows). A single column name returns a flat list of
    scalars; a list of column names returns a list of tuples. Keeps City
    Cut from showing statistically meaningless tiny-volume rows - a city
    with 2 orders and a 100% or 0% FASR is noise, not signal. min_orders=0
    shows everything.

    search_terms, when given (and group_cols is a single column), bypasses
    min_orders entirely and returns only values matching any term (see
    _search_mask) - an explicit "I know exactly what I'm looking for" query
    should never be filtered out or capped, however low its volume."""
    cols = group_cols if isinstance(group_cols, list) else [group_cols]
    totals = cube.groupby(cols, observed=True)["Flag"].sum().sort_values(ascending=False)
    if search_terms and len(cols) == 1:
        return totals[_search_mask(totals.index, search_terms)].index.tolist()
    totals = totals[totals >= min_orders]
    return totals.index.tolist()


def discover_channels(df: pd.DataFrame) -> list[str]:
    return sorted(df["channel_name"].dropna().unique().tolist())


def discover_brands(df: pd.DataFrame) -> list[str]:
    return sorted(df["Brand"].dropna().unique().tolist())


def discover_payment_modes(df: pd.DataFrame) -> list[str]:
    return sorted(df["Payment mode"].dropna().unique().tolist())


def discover_states(df: pd.DataFrame) -> list[str]:
    return sorted(df["State"].dropna().unique().tolist())


def discover_statuses(df: pd.DataFrame) -> list[str]:
    return sorted(df["Final Status2"].dropna().unique().tolist())


def discover_couriers_alpha(df: pd.DataFrame) -> list[str]:
    """Alphabetical courier list, for the sidebar Courier filter checkbox
    list - matches Channel/Payment/State/Status's ordering convention."""
    return sorted(df["Grouped_Courier_v2"].dropna().unique().tolist())


def aggregate_metrics(df: pd.DataFrame, start=None, end=None) -> dict:
    """Single-number metrics + order count over an optional date window -
    used for KPI cards, period-over-period deltas, and Reports anomaly
    detection."""
    valid = df.dropna(subset=["first_ofd_date"])
    if start is not None:
        valid = valid[valid["first_ofd_date"] >= start]
    if end is not None:
        valid = valid[valid["first_ofd_date"] <= end]
    orders = valid["Flag"].sum()
    if orders == 0:
        return {
            "FASR %": None, "RTO %": None, "RTO % (Overall)": None, "NDR %": None,
            "NDR to Delivery %": None,
            "Orders": 0, "NDR Orders": 0,
        }
    ndr_total = valid["NDR"].sum()
    return {
        "FASR %": valid["FASR"].sum() / orders,
        "RTO %": valid["RTO"].sum() / orders,
        "RTO % (Overall)": valid["RTO_OVERALL"].sum() / orders,
        "NDR %": valid["NDR"].sum() / orders,
        "NDR to Delivery %": (valid["NDR_TO_DELIVERY"].sum() / ndr_total) if ndr_total > 0 else None,
        "Orders": int(orders),
        "NDR Orders": int(ndr_total),
    }


def aggregate_metrics_cube(cube: pd.DataFrame, start=None, end=None) -> dict:
    """Cube version of aggregate_metrics."""
    valid = cube
    if start is not None:
        valid = valid[valid["first_ofd_date"] >= start]
    if end is not None:
        valid = valid[valid["first_ofd_date"] <= end]
    orders = valid["Flag"].sum()
    if orders == 0:
        return {
            "FASR %": None, "RTO %": None, "RTO % (Overall)": None, "NDR %": None,
            "NDR to Delivery %": None,
            "Orders": 0, "NDR Orders": 0,
        }
    ndr_total = valid["NDR"].sum()
    return {
        "FASR %": valid["FASR"].sum() / orders,
        "RTO %": valid["RTO"].sum() / orders,
        "RTO % (Overall)": valid["RTO_OVERALL"].sum() / orders,
        "NDR %": valid["NDR"].sum() / orders,
        "NDR to Delivery %": (valid["NDR_TO_DELIVERY"].sum() / ndr_total) if ndr_total > 0 else None,
        "Orders": int(orders),
        "NDR Orders": int(ndr_total),
    }


def group_summary(df: pd.DataFrame, group_col: str, groups: list[str], start=None, end=None) -> pd.DataFrame:
    """One row per group, aggregated over the given date window - the input
    for sort_groups()'s row ordering. Vectorized: one groupby covering
    every group in `groups`, not one aggregate_metrics call per group.
    Covers every METRICS label (not just a hardcoded few) so any metric is
    available as a sort option. Pure sum-ratios (no averaging), so this
    already works unchanged on either the raw per-order df or the
    pre-aggregated cube - no separate cube variant needed."""
    valid = df.dropna(subset=["first_ofd_date"])
    if start is not None:
        valid = valid[valid["first_ofd_date"] >= start]
    if end is not None:
        valid = valid[valid["first_ofd_date"] <= end]
    # The final result only ever keeps `groups` (reindex below drops
    # everything else anyway) - filtering here first means the groupby only
    # scans rows that matter, instead of every row in `valid` (which, for a
    # high-cardinality dimension like Pincode, can be the large majority of
    # the whole cube). Identical output, far less work whenever `groups` is
    # a strict subset (any min_orders filter, or the City/Pincode row cap).
    valid = valid[valid[group_col].isin(groups)]

    cols = _metrics_columns(METRICS)
    grouped = valid.groupby(group_col)[cols].sum()
    summary = pd.DataFrame(_rows_from_grouped(grouped, METRICS))
    summary["Orders"] = grouped["Flag"]
    return summary.reindex(groups)



# "Default" keeps whatever order was passed in (e.g. priority groups
# pinned to top, then by volume); "Alphabetical" and every METRICS label +
# Orders, in both directions, are always available - sort_groups() derives
# whichever column is named from the "(High to Low)"/"(Low to High)"
# suffix, so this list and that logic can never drift apart.
SORT_OPTIONS = (
    ["Default"]
    + [f"{label} (High to Low)" for label, _, _ in METRICS]
    + [f"{label} (Low to High)" for label, _, _ in METRICS]
    + ["Orders (High to Low)", "Orders (Low to High)", "Alphabetical"]
)


def sort_groups(summary: pd.DataFrame, sort_by: str, default_order: list[str]) -> list[str]:
    """Reorder a list of group names (couriers, payment modes, etc.) per a
    chosen sort option, using already-computed group_summary metrics (any
    METRICS label, or Orders). 'Default' keeps whatever order was passed
    in; 'Alphabetical' sorts the names themselves; anything else must be a
    "<metric> (High to Low)"/"(Low to High)" pair matching a summary
    column, falling back to default_order if the metric or table is
    unrecognized/empty."""
    if sort_by == "Default" or summary.empty:
        return default_order
    if sort_by == "Alphabetical":
        return sorted(default_order)
    if sort_by.endswith(" (High to Low)"):
        metric, ascending = sort_by[: -len(" (High to Low)")], False
    elif sort_by.endswith(" (Low to High)"):
        metric, ascending = sort_by[: -len(" (Low to High)")], True
    else:
        return default_order
    if metric not in summary.columns:
        return default_order
    return summary.sort_values(metric, ascending=ascending, na_position="last").index.tolist()


# ---- Wide (sheet-style) cut tables: rows = metric, columns = date ----------

def _week_start(dates: pd.Series) -> pd.Series:
    """Start-of-bucket Timestamp for the Weekly view: "Week N" means the
    Nth 7-day block counted from Jan 1 of that date's year (resets every
    Jan 1, however many days are left in the final partial week) - NOT a
    calendar/ISO week (which starts on a fixed weekday and can spill across
    a year boundary), since the user explicitly wants W1, W2, ... in
    chronological order starting from Jan 1. Returns a Timestamp per row
    (the shared anchor date for that row's week), so it slots into every
    existing bucket/groupby/sort call unchanged - only the display label
    (see _week_label/format_wide_for_display) knows these are "weeks"."""
    year_start = pd.to_datetime(dates.dt.year.astype(str) + "-01-01")
    week_index = (dates - year_start).dt.days // 7
    return year_start + pd.to_timedelta(week_index * 7, unit="D")


def _week_label(week_start: pd.Timestamp) -> int:
    """1-based week number within week_start's own year - always exact
    since week_start is itself one of _week_start()'s anchor dates."""
    year_start = pd.Timestamp(year=week_start.year, month=1, day=1)
    return int((week_start - year_start).days // 7) + 1


def _bucket_key(valid: pd.DataFrame, granularity: str) -> pd.Series:
    """Column to group by: calendar day (existing behaviour), calendar
    month, or Week N-from-Jan-1 (see _week_start), for the Daily/Weekly/
    Monthly view toggle."""
    if granularity == "month":
        return valid["first_ofd_date"].dt.to_period("M")
    if granularity == "week":
        return _week_start(valid["first_ofd_date"])
    return valid["first_ofd_date"]


def _metrics_columns(metrics_list) -> list[str]:
    """Every raw sum-column (numerator + denominator) a metrics list
    touches - used to do ONE groupby().sum() covering every metric at
    once, instead of one groupby call per metric."""
    cols = set()
    for _, num, denom in metrics_list:
        cols.add(num)
        cols.add(denom)
    cols.add("Flag")
    return sorted(cols)


def _rows_from_grouped(grouped: pd.DataFrame, metrics_list) -> dict:
    """grouped: a date-indexed (or (group, date)-indexed) DataFrame already
    summed via _metrics_columns - derive every metric's ratio via
    vectorized division, no further groupby needed."""
    return {label: grouped[num] / grouped[denom] for label, num, denom in metrics_list}


def overall_wide(df: pd.DataFrame, granularity: str = "day") -> pd.DataFrame:
    """Rows: FASR %, RTO in 1st Att %, RTO % (Overall), NDR %, NDR to
    Delivery/RTO/Pending %, Orders. Columns: dates, or months if
    granularity='month'."""
    valid = df.dropna(subset=["first_ofd_date"])
    cols = _metrics_columns(METRICS)
    grouped = valid.groupby(_bucket_key(valid, granularity))[cols].sum()
    rows = _rows_from_grouped(grouped, METRICS)
    rows["Orders"] = grouped["Flag"]
    table = pd.DataFrame(rows).T
    return table.reindex(columns=sorted(table.columns))


def overall_wide_cube(cube: pd.DataFrame, granularity: str = "day") -> pd.DataFrame:
    """Cube version of overall_wide."""
    cols = _metrics_columns(METRICS)
    grouped = cube.groupby(_bucket_key(cube, granularity))[cols].sum()
    rows = _rows_from_grouped(grouped, METRICS)
    rows["Orders"] = grouped["Flag"]
    table = pd.DataFrame(rows).T
    return table.reindex(columns=sorted(table.columns))


# Same "always every METRICS label, both directions" idea as SORT_OPTIONS,
# for the 2D Cut tab's outer-group ordering.
MATRIX_SORT_OPTIONS = (
    ["Orders (High to Low)", "Orders (Low to High)"]
    + [f"{label} (High to Low)" for label, _, _ in METRICS]
    + [f"{label} (Low to High)" for label, _, _ in METRICS]
    + ["Alphabetical"]
)


def discover_grouped_rows(
    cube: pd.DataFrame, row_col: str, col_col: str,
    min_orders: int = 0, sort_by: str = "Orders (High to Low)",
    max_outer_groups: int | None = None, search_terms: list[str] | None = None,
) -> tuple[list[tuple], int]:
    """Ordered (row_col, col_col) pairs for the 2D Cut tab's rowspan-grouped
    rows (outer group = whichever dimension has more distinct values, inner
    sub-row = the other - see group_orders_fasr_wide). Outer groups
    (row_col) are filtered to those with at least min_orders TOTAL orders
    (across every col_col value, 0 = show every row) and ordered per
    sort_by (see MATRIX_SORT_OPTIONS - any METRICS label or Orders, either
    direction; unrecognized/"Default" falls back to Orders High to Low).
    Every col_col value actually present for a qualifying outer group is
    then listed under it, largest first - so e.g. Mumbai and Delhi can
    show a different set/order of couriers if their volumes differ.

    max_outer_groups, when given, keeps only the top-ranked outer groups
    (by whatever sort_by already ordered them) - callers use this for
    City/Pincode, where min_orders=0 can otherwise mean thousands of outer
    groups. Returns (pairs, total_outer_groups) - the second value is the
    count BEFORE capping, so callers can show a "showing top N of TOTAL"
    note; with max_outer_groups=None it just equals the number of outer
    groups actually returned.

    search_terms, when given, bypasses BOTH min_orders and max_outer_groups
    for the outer dimension - only rows whose row_col value matches (see
    _search_mask) survive, however few or low-volume. Still ordered by
    sort_by, same as any other result."""
    cols = _metrics_columns(METRICS)
    row_totals = cube.groupby(row_col)[cols].sum()
    if search_terms:
        row_totals = row_totals[_search_mask(row_totals.index, search_terms)]
    else:
        row_totals = row_totals[row_totals["Flag"] >= min_orders]
    row_ratios = _rows_from_grouped(row_totals, METRICS)

    if sort_by == "Alphabetical":
        row_order = sorted(row_totals.index.tolist())
    elif sort_by == "Orders (Low to High)":
        row_order = row_totals["Flag"].sort_values(ascending=True).index.tolist()
    elif sort_by.endswith(" (High to Low)") and sort_by[: -len(" (High to Low)")] in row_ratios:
        metric = sort_by[: -len(" (High to Low)")]
        row_order = row_ratios[metric].sort_values(ascending=False, na_position="last").index.tolist()
    elif sort_by.endswith(" (Low to High)") and sort_by[: -len(" (Low to High)")] in row_ratios:
        metric = sort_by[: -len(" (Low to High)")]
        row_order = row_ratios[metric].sort_values(ascending=True, na_position="last").index.tolist()
    else:
        row_order = row_totals["Flag"].sort_values(ascending=False).index.tolist()

    total_outer_groups = len(row_order)
    if not search_terms and max_outer_groups is not None:
        row_order = row_order[:max_outer_groups]

    # Only scan rows for outer groups actually being kept - same "filter
    # before the expensive groupby" idea as group_summary/grouped_wide_cube.
    # Without this, a capped Pincode cut would still pay the FULL 2-key
    # groupby over every pincode before throwing away everything past the
    # cap - the exact cost the cap is meant to avoid.
    filtered_cube = cube[cube[row_col].isin(row_order)]
    pair_totals = filtered_cube.groupby([row_col, col_col])["Flag"].sum()
    # Precomputed once as a set, not recomputed (and rescanned) inside the
    # loop below - get_level_values(0) is an O(n) scan over every pair row,
    # and doing that once per outer group turned into a real quadratic
    # blowup for a high-cardinality outer dimension (Pincode: ~9k outer
    # groups x ~35k pair rows = ~280M comparisons, measured at 112s before
    # this fix; City's much smaller cardinality had masked the same bug).
    outer_values_with_pairs = set(pair_totals.index.get_level_values(0))
    pairs = []
    for row_val in row_order:
        if row_val in outer_values_with_pairs:
            sub = pair_totals.loc[row_val].sort_values(ascending=False)
            pairs.extend((row_val, col_val) for col_val in sub.index)
    return pairs, total_outer_groups


def group_orders_fasr_wide(
    df: pd.DataFrame, row_col: str, col_col: str, groups: list[tuple], granularity: str = "day",
    metrics_list=None, include_orders: bool = True,
) -> pd.DataFrame:
    """2D Cut table: rows are a (outer dimension, inner dimension)
    MultiIndex - rowspan-grouped by the first level, same rendering idea as
    grouped_wide_cube - for every pair in `groups` (see
    discover_grouped_rows), in order. Columns are a (date, metric)
    MultiIndex: Orders (if include_orders) then every metric in
    metrics_list, per date - the user picks which via the 2D Cut tab's
    metric checklist, same idea as the 1D Cut tab's metrics_list.

    Filters df down to only the (row_col, col_col) pairs in `groups` BEFORE
    the groupby, rather than computing every pair the cube has and
    reindexing down afterward - a high-cardinality dimension's cube can
    hold far more combinations than min_orders keeps, and grouping by a
    3rd key (date) on top of that would otherwise be the expensive part;
    trimming first keeps this cheap even though dates are columns."""
    metrics_list = metrics_list or []
    valid = df.dropna(subset=["first_ofd_date"])
    pair_index = pd.MultiIndex.from_tuples(groups, names=[row_col, col_col])
    mask = pd.MultiIndex.from_arrays([valid[row_col], valid[col_col]]).isin(pair_index)
    valid = valid[mask]

    bucket = _bucket_key(valid, granularity)
    needed_cols = sorted(set(_metrics_columns(metrics_list)) | {"Flag"})
    grouped = valid.groupby([row_col, col_col, bucket])[needed_cols].sum()
    dates = sorted(grouped.index.get_level_values(-1).unique())

    orders_wide = grouped["Flag"].unstack(level=-1).reindex(index=groups, columns=dates) if include_orders else None
    metric_wides = {
        label: (grouped[num] / grouped[denom]).unstack(level=-1).reindex(index=groups, columns=dates)
        for label, num, denom in metrics_list
    }

    data = {}
    col_tuples = []
    for date in dates:
        if include_orders:
            col_tuples.append((date, "Orders"))
            data[(date, "Orders")] = orders_wide[date]
        for label, wide in metric_wides.items():
            col_tuples.append((date, label))
            data[(date, label)] = wide[date]

    table = pd.DataFrame(data, index=pd.MultiIndex.from_tuples(groups, names=["Group", "Metric"]))
    table.columns = pd.MultiIndex.from_tuples(col_tuples)
    return table


def format_orders_fasr_for_display(table: pd.DataFrame, granularity: str = "day") -> pd.DataFrame:
    """Formats a group_orders_fasr_wide table: Orders sub-columns as
    comma-grouped integers, FASR % sub-columns as percentages, date labels
    shortened the same way format_wide_for_display does. Columns are a
    (date, metric) MultiIndex here, so this doesn't reuse
    format_wide_for_display directly."""
    out = table.copy().astype(object)
    for date, metric in table.columns:
        if metric == "Orders":
            out[(date, metric)] = table[(date, metric)].map(lambda v: f"{int(v):,}" if pd.notna(v) else "-")
        else:
            out[(date, metric)] = table[(date, metric)].map(lambda v: f"{v * 100:.1f}%" if pd.notna(v) else "-")

    dates = table.columns.get_level_values(0)
    if granularity == "week":
        span_years = len({d.year for d in set(dates)}) if len(dates) else 1
        date_labels = {
            d: (f"{d.year}-W{_week_label(d)}" if span_years > 1 else f"W{_week_label(d)}") for d in set(dates)
        }
    elif len(dates) and isinstance(dates[0], pd.Period):
        date_labels = {d: d.strftime("%b %Y") for d in set(dates)}
    else:
        span_years = len({d.year for d in set(dates)}) if len(dates) else 1
        fmt = "%d %b %Y" if span_years > 1 else "%d %b"
        date_labels = {d: d.strftime(fmt) for d in set(dates)}
    out.columns = pd.MultiIndex.from_tuples([(date_labels[d], m) for d, m in table.columns])
    return out


def grouped_wide(df: pd.DataFrame, group_col: str, groups: list[str], granularity: str = "day") -> pd.DataFrame:
    """Rows: MultiIndex (group, metric) stacked for every group in `groups`,
    in the order given. Columns: dates, or months if granularity='month'.
    Used for Payment Cut (COD/PPD together) and Delivery Cut (all couriers,
    priority ones first). Computes every group's sums in ONE combined
    groupby (not one groupby per group) for performance."""
    valid = df.dropna(subset=["first_ofd_date"])
    cols = _metrics_columns(METRICS)
    bucket = _bucket_key(valid, granularity)
    grouped_all = valid.groupby([group_col, bucket])[cols].sum()

    frames = []
    for grp in groups:
        if grp in grouped_all.index.get_level_values(0):
            g = grouped_all.loc[grp]
        else:
            g = pd.DataFrame(columns=cols)
        rows = _rows_from_grouped(g, METRICS)
        rows["Orders"] = g["Flag"] if not g.empty else pd.Series(dtype=float)
        t = pd.DataFrame(rows).T
        t.index = pd.MultiIndex.from_product([[grp], t.index], names=["Group", "Metric"])
        frames.append(t)
    combined = pd.concat(frames)
    return combined.reindex(columns=sorted(combined.columns))


def grouped_wide_cube(
    cube: pd.DataFrame, group_col: str, groups: list[str], granularity: str = "day",
    metrics_list=None, include_orders: bool = True,
) -> pd.DataFrame:
    """Cube version of grouped_wide. metrics_list defaults to the full
    METRICS set (unchanged behavior for existing callers); the 1D Cut tab
    passes whatever subset the user actually checked, so the table only
    grows the rows that were asked for. include_orders=False drops the
    Orders row too, for a caller that only wants rate metrics."""
    metrics_list = METRICS if metrics_list is None else metrics_list
    cols = _metrics_columns(metrics_list)
    # Same "only scan rows that matter" optimization as group_summary - rows
    # belonging to any other value of group_col are pure wasted groupby
    # work, and for a high-cardinality dimension (Pincode/Lane) that waste
    # can dwarf the useful part.
    cube = cube[cube[group_col].isin(groups)]
    bucket = _bucket_key(cube, granularity)
    grouped_all = cube.groupby([group_col, bucket])[cols].sum()
    dates = sorted(grouped_all.index.get_level_values(1).unique())

    # Vectorized (unstack, not a per-group .loc + concat loop) - the
    # previous version did one .loc[grp] lookup, one DataFrame().T, and one
    # MultiIndex.from_product PER group, then concatenated them all: fine
    # for a handful of states, but for a high-cardinality dimension like
    # Lane (60k+ distinct values) that per-group Python-level overhead and
    # the final concat of tens of thousands of tiny frames dominated
    # completely - measured at 18+ minutes for Lane. Building one
    # (group x date) wide frame per metric via unstack, then stacking them
    # with a single numpy reshape, does the exact same computation in
    # under a second.
    row_order = (["Orders"] if include_orders else []) + [m[0] for m in metrics_list]
    wides = {}
    if include_orders:
        wides["Orders"] = grouped_all["Flag"].unstack(level=-1).reindex(index=groups, columns=dates)
    for label, num, denom in metrics_list:
        wides[label] = (grouped_all[num] / grouped_all[denom]).unstack(level=-1).reindex(index=groups, columns=dates)

    if not groups:
        return pd.DataFrame(columns=dates, index=pd.MultiIndex.from_arrays([[], []], names=["Group", "Metric"]))
    # np.stack + reshape (not pd.concat) builds the (Group, Metric)-major
    # row order directly: axis=1 interleaves each group's metrics together
    # (group0-metric0, group0-metric1, ..., group1-metric0, ...), matching
    # MultiIndex.from_product([groups, row_order])'s iteration order
    # exactly, with no per-row Python overhead either way.
    stacked = np.stack([wides[m].to_numpy() for m in row_order], axis=1)
    reshaped = stacked.reshape(len(groups) * len(row_order), len(dates))
    index = pd.MultiIndex.from_product([groups, row_order], names=["Group", "Metric"])
    return pd.DataFrame(reshaped, index=index, columns=dates)


# ---- Attempt Analysis: same idea, separate tab so the main dashboard -------
# ---- doesn't get cluttered with the attempt-by-attempt breakdown ----------

_ATTEMPT_CONDITIONAL_PAIRS = [
    ("2nd Att Del %", "DELIVERED_2ND", "RTO_2ND"),
    ("3rd Att Del %", "DELIVERED_3RD", "RTO_3RD"),
    ("3+ Att Del %", "DELIVERED_3PLUS", "RTO_3PLUS"),
]

# 1D Cut's metric checklist for Attempt Analysis (dimension-based cuts) -
# every ATTEMPT_METRICS row, the 3 conditional pairs, plus Orders.
ATTEMPT_SELECTABLE_METRIC_LABELS = (
    [label for label, _, _ in ATTEMPT_METRICS] + [label for label, _, _ in _ATTEMPT_CONDITIONAL_PAIRS] + ["Orders"]
)
# 2D Cut's metric checklist is narrower: group_orders_fasr_wide only knows
# the (label, num, denom) shape, and the conditional pairs' denominator
# isn't a single column (it's DELIVERED_2ND + RTO_2ND summed) - so those 3
# are left out here rather than bolting a second code path onto
# group_orders_fasr_wide for a rarely-needed combination.
ATTEMPT_2D_SELECTABLE_METRIC_LABELS = [label for label, _, _ in ATTEMPT_METRICS] + ["Orders"]

# Attempt Analysis Overall's "double block" layout (see reference image):
# rows grouped by Attempt Number, each with its own Delivered/RTO/Pending %
# sub-rows, plus a standalone Orders row up top and a trailing "Summary"
# group for the cross-attempt totals. (source_label, display_label) pairs -
# source_label is the key _attempt_rows_from_grouped() already computes;
# display_label is what's shown (stripped of the "1st Attempt: " prefix
# that's now redundant with the Group column).
ATTEMPT_DOUBLE_BLOCK = [
    ("Overall", [("Orders", "Orders")]),
    ("1st Attempt", [("1st Attempt: FASR %", "FASR %"), ("1st Attempt: RTO %", "RTO %"), ("1st Attempt: Pending %", "Pending %")]),
    ("2nd Attempt", [("2nd Attempt: Delivered %", "Delivered %"), ("2nd Attempt: RTO %", "RTO %"), ("2nd Attempt: Pending %", "Pending %")]),
    ("3rd Attempt", [("3rd Attempt: Delivered %", "Delivered %"), ("3rd Attempt: RTO %", "RTO %"), ("3rd Attempt: Pending %", "Pending %")]),
    ("3+ Attempts", [("3+ Attempts: Delivered %", "Delivered %"), ("3+ Attempts: RTO %", "RTO %"), ("3+ Attempts: Pending %", "Pending %")]),
    ("Summary", [
        ("Delivered % (Overall)", "Delivered % (Overall)"), ("RTO % (Overall)", "RTO % (Overall)"),
        ("NDR %", "NDR %"), ("NDR to Delivery %", "NDR to Delivery %"),
        ("2nd Att Del %", "2nd Att Del %"), ("3rd Att Del %", "3rd Att Del %"), ("3+ Att Del %", "3+ Att Del %"),
    ]),
]


def _attempt_columns() -> list[str]:
    cols = set(_metrics_columns(ATTEMPT_METRICS))
    for _, num, alt in _ATTEMPT_CONDITIONAL_PAIRS:
        cols.add(num)
        cols.add(alt)
    return sorted(cols)


def _attempt_rows_from_grouped(grouped: pd.DataFrame) -> dict:
    rows = _rows_from_grouped(grouped, ATTEMPT_METRICS)
    for label, num, alt in _ATTEMPT_CONDITIONAL_PAIRS:
        denom = grouped[num] + grouped[alt]
        rows[label] = (grouped[num] / denom).where(denom > 0)
    rows["Orders"] = grouped["Flag"]
    return rows


def attempt_overall_grouped_wide(cube: pd.DataFrame, granularity: str = "day") -> pd.DataFrame:
    """Attempt Analysis's Overall tab, in the 'double block' layout (see
    ATTEMPT_DOUBLE_BLOCK): rows grouped by Attempt Number, rowspan-grouped
    the same way as grouped_wide_cube. Reuses _attempt_rows_from_grouped's
    existing (verified) numbers unchanged - this only reorganizes them into
    a (Group, Metric) MultiIndex for display, it doesn't recompute
    anything."""
    cols = _attempt_columns()
    bucket = _bucket_key(cube, granularity)
    grouped = cube.groupby(bucket)[cols].sum()
    flat = _attempt_rows_from_grouped(grouped)

    frames = []
    for group_label, pairs in ATTEMPT_DOUBLE_BLOCK:
        for src_label, disp_label in pairs:
            t = pd.DataFrame({disp_label: flat[src_label]}).T
            t.index = pd.MultiIndex.from_tuples([(group_label, disp_label)], names=["Group", "Metric"])
            frames.append(t)
    combined = pd.concat(frames)
    return combined.reindex(columns=sorted(combined.columns))


def attempt_grouped_wide(df: pd.DataFrame, group_col: str, groups: list[str], granularity: str = "day", metrics_list=None) -> pd.DataFrame:
    """Attempt-cascade version of grouped_wide - see ATTEMPT_METRICS.
    metrics_list (a subset of ATTEMPT_SELECTABLE_METRIC_LABELS) restricts
    which rows are kept, for the 1D Cut tab's metric checklist; None keeps
    everything (unchanged default behavior)."""
    # Vectorized (unstack, not a per-group .loc + concat loop) - see
    # grouped_wide_cube's comment for why: the old per-group version was
    # fine for a handful of states but took 18+ minutes for Lane (60k+
    # distinct values).
    valid = df.dropna(subset=["first_ofd_date"])
    valid = valid[valid[group_col].isin(groups)]
    cols = _attempt_columns()
    bucket = _bucket_key(valid, granularity)
    grouped_all = valid.groupby([group_col, bucket])[cols].sum()
    dates = sorted(grouped_all.index.get_level_values(1).unique())

    row_order = [label for label, _, _ in ATTEMPT_METRICS] + [label for label, _, _ in _ATTEMPT_CONDITIONAL_PAIRS] + ["Orders"]
    if metrics_list is not None:
        row_order = [m for m in row_order if m in metrics_list]
    if not groups or not row_order or not dates:
        return pd.DataFrame(columns=dates, index=pd.MultiIndex.from_arrays([[], []], names=["Group", "Metric"]))

    wides = {}
    for label, num, denom in ATTEMPT_METRICS:
        if label in row_order:
            wides[label] = (grouped_all[num] / grouped_all[denom]).unstack(level=-1).reindex(index=groups, columns=dates)
    for label, num, alt in _ATTEMPT_CONDITIONAL_PAIRS:
        if label in row_order:
            cond_denom = grouped_all[num] + grouped_all[alt]
            ratio = (grouped_all[num] / cond_denom).where(cond_denom > 0)
            wides[label] = ratio.unstack(level=-1).reindex(index=groups, columns=dates)
    if "Orders" in row_order:
        wides["Orders"] = grouped_all["Flag"].unstack(level=-1).reindex(index=groups, columns=dates)

    stacked = np.stack([wides[m].to_numpy() for m in row_order], axis=1)
    reshaped = stacked.reshape(len(groups) * len(row_order), len(dates))
    index = pd.MultiIndex.from_product([groups, row_order], names=["Group", "Metric"])
    return pd.DataFrame(reshaped, index=index, columns=dates)


def format_wide_for_display(table: pd.DataFrame, granularity: str = "day") -> pd.DataFrame:
    """Turn a numeric wide table into a display-ready table of formatted
    strings: percentages as '78.6%', Orders as '21,427', Average Delivery
    Days as a plain whole number, dates (or weeks, or months) as short
    labels across the top."""
    if isinstance(table.index, pd.MultiIndex):
        metric_level = table.index.get_level_values(-1)
    else:
        metric_level = table.index

    # Built as a list of formatted rows and turned into a DataFrame in one
    # shot, rather than assigning row-by-row via `out.iloc[i] = ...` - that
    # goes through pandas' slow mixed-dtype setitem path once per row, and
    # its cost scales with column count, which made wide (day-granularity,
    # ~240-column) tables take seconds instead of milliseconds to format.
    def _fmt_row(metric, row):
        if metric == "Orders":
            return [f"{int(v):,}" if pd.notna(v) else "-" for v in row]
        else:
            return [f"{v * 100:.1f}%" if pd.notna(v) else "-" for v in row]

    rows = [_fmt_row(metric, row) for metric, row in zip(metric_level, table.itertuples(index=False, name=None))]
    out = pd.DataFrame(rows, index=table.index, columns=table.columns)

    if granularity == "week":
        span_years = len({c.year for c in table.columns}) if len(table.columns) else 1
        out.columns = [
            (f"{c.year}-W{_week_label(c)}" if span_years > 1 else f"W{_week_label(c)}") for c in table.columns
        ]
    elif len(table.columns) and isinstance(table.columns[0], pd.Period):
        out.columns = [c.strftime("%b %Y") for c in table.columns]
    else:
        span_years = len({c.year for c in table.columns}) if len(table.columns) else 1
        fmt = "%d %b %Y" if span_years > 1 else "%d %b"
        out.columns = [c.strftime(fmt) for c in out.columns]
    return out


