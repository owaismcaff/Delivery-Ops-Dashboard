# Daily FASR / RTO / NDR Dashboard

A local business dashboard: upload each day's raw CSV export and it merges
into a running history. Shows KPI cards with period-over-period deltas, a
FASR/RTO/NDR trend chart with order volume underneath, and three
comparison tabs (Payment, Delivery, Channel) - each with a ranked bar
chart plus the full sheet-style breakdown table.

## Setup (one-time)

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

This starts a local web server and opens the dashboard in your browser at
**http://localhost:8501**. Leave the terminal window open while you're using
the dashboard; close it (Ctrl+C) to stop the server.

## Upload size limit

Streamlit defaults to a 200MB upload cap. `.streamlit/config.toml` in this
folder raises that to 5GB, which should comfortably cover your raw exports
(~450-500MB today). If your exports grow past that, bump `maxUploadSize` in
that file (it's in MB) and restart the app.

## First-time setup: seed with history

Before uploading your regular daily exports, upload `seed_original_raw.csv`
from the sidebar first (you can select it together with your first real
export - the uploader accepts multiple files at once). This is the Raw sheet
data from your original workbook (Jul 1-9), pulled directly out of the
.xlsb.

Why this matters: any fresh raw export only contains orders placed from that
export's start date onward, so the *first* day or two in a brand new export
looks artificially bad (very few orders have had time to reach their first
out-for-delivery attempt yet). Seeding with the original workbook's history
first means those older, already-resolved orders stay in the dashboard;
your daily uploads then only add new orders and refresh the status of ones
that changed.

## Recent fixes/additions

- **Performance** - table generation and the Reports scan were both doing
  a separate pandas operation per metric/segment (dozens of redundant
  passes over the data) and the loaded store was using a PyArrow-backed
  string dtype that made repeated filtering ~800ms per call instead of
  ~10ms. Fixed both: every table now uses one combined `groupby` instead
  of one per metric, and the store's string columns are converted to
  plain object dtype on load. Full page interactions went from ~40-60s
  down to ~4-12s.
- **Row order** - a "Row order" dropdown on Payment Cut and Delivery Cut
  (both the Dashboard tab and Attempt Analysis) lets you reorder rows by
  FASR%/RTO%(Overall)/NDR% (worst first), Orders (highest first), or
  alphabetically - instead of always the default (priority couriers
  pinned to top, then by volume).
- **Reports methodology overhaul** - severity is now based on *relative*
  change (% change vs. the metric's own baseline) instead of raw
  percentage points, since 1pp means something very different for a
  metric that sits at 5% vs. one that sits at 80%. A separate **Impact %**
  column (volume-weighted: |change| x segment orders / total orders) is
  used purely for ranking, so a huge swing on a tiny segment doesn't
  outrank a moderate swing on a segment carrying half your volume. Each
  (segment, metric) pair is flagged independently now, rather than only
  showing a segment's single worst metric. Also added a **per-metric
  maturity buffer**: FASR%/RTO%(1st attempt) resolve within a single
  attempt so barely need one, but RTO%(Overall)/NDR%/NDR-to-Delivery%
  depend on potentially several re-attempts and were verified to stay
  understated for roughly 9 days after first_ofd_date - without the
  buffer, comparing them right up against today's still-resolving data
  flagged nearly everything as a false dip. A new **Delivery speed**
  section (separate table, ranked by "Extra Order-Days" rather than
  Impact %, since it's not a rate) flags segments whose Average Delivery
  Days has slowed down significantly.
- **Dark mode readability** - the custom tables and KPI cards were built
  with text colors tuned for a light background but no explicit background
  of their own, so in dark mode they'd inherit Streamlit's dark page
  background while keeping dark text (or vice versa) - nearly invisible.
  Fixed by giving them an explicit, fixed light background regardless of
  Streamlit's active theme, so they always render as readable light cards
  no matter what mode the rest of the app is in.
- **CSV download** - every table (Dashboard's Overall/Payment Cut/Delivery
  Cut, same three in Attempt Analysis, and both Reports tables) has a
  "Download CSV" button right below it, exporting exactly what's on
  screen (respecting the current Daily/Monthly view, row order, and
  filters).
- **Average Delivery Days** now shows 1 decimal place (e.g. "1.2") instead
  of rounding to a whole number.
- **Color palette** - unified across tables, KPI cards, and charts into one
  consistent professional set (slate neutrals + green/red/amber/indigo
  semantic accents, all the same visual "weight") instead of the previous
  mixed palette.

## Layout

Three top-level tabs:

### Dashboard (unchanged)

- **KPI row** - Orders, FASR%, RTO% (1st attempt), RTO% (Overall), NDR% for
  the selected date range, each with a delta vs. the immediately preceding
  period of the same length.
- **Trend chart** - FASR/RTO/NDR % over time, with daily order volume as a
  bar chart underneath.
- **Overall / Payment Cut / Delivery Cut sub-tabs** - each a sheet-style
  table (dates across, metrics down): FASR %, RTO in 1st Att %, RTO %
  (Overall), NDR %, NDR to Delivery/RTO/Pending %, Average Delivery Days
  (NDR-then-delivered orders only, calendar days), Orders. Payment Cut and
  Delivery Cut also get a bar chart ranking COD/PPD (or couriers) best-to-
  worst by FASR% for the selected range.

### Attempt Analysis (new)

A separate attempt-by-attempt breakdown, matching your reference sheet's
structure, kept on its own tab so the main Dashboard doesn't get
cluttered. Same Overall / Payment Cut / Delivery Cut sub-tab structure,
but every stage row uses the **same denominator** (total orders in scope)
rather than a shrinking sub-population - so the percentages are literal
slices of one 100% pie:

- **1st Attempt: FASR % / RTO % / Pending %** - of all orders. Pending % is
  orders still sitting at attempt 1, unresolved (hasn't had a 2nd attempt
  yet).
- **2nd Attempt: Delivered % / RTO % / Pending %** - of all orders, the
  ones whose *current* attempt count is exactly 2. Pending % = still stuck
  there, hasn't moved to a 3rd attempt yet.
- **3rd Attempt: Delivered % / RTO % / Pending %** - same, for exactly
  attempt 3.
- **3+ Attempts: Delivered % / RTO % / Pending %** - same, for more than 3
  attempts (one combined bucket, not split out attempt-by-attempt further).
- **Delivered % (Overall)** - FASR + 2nd + 3rd + 3+ Attempt Delivered %
  added together = overall delivery rate regardless of attempt count.
- **RTO % (Overall) / NDR %** - same as the Dashboard tab (NDR % here is
  the existing *broad* definition - didn't resolve on the 1st attempt,
  whether it later got delivered, RTO'd, or is still pending - not the
  same thing as the narrower per-stage "Pending %" rows above).
- **NDR to Delivery %** - of the NDR bucket, how many eventually got
  delivered (same as the Dashboard tab).
- **2nd/3rd/3+ Att Del %** - a *different* kind of percentage: conditional
  on orders actually resolved at that specific attempt (Delivered ÷
  (Delivered + RTO) for that attempt only, excluding ones still pending) -
  answers "given a re-attempt happened, how often did it succeed."

Every stage's Delivered % + RTO % + Pending % sums to exactly that stage's
share of total orders (a clean 3-way split, not an approximation) - so all
12 stage-level rows (1st through 3+) sum to ~100% overall, typically within
a hundredth of a percent. Any tiny remaining gap is an attempt-count-0 data
edge case (a handful of orders with no attempts recorded yet but already
showing a final status) - negligible, not a calculation error.

### Reports (new)

Automatic dip/anomaly detection. Scans **State**, **Delivery Partner**,
**Payment mode**, and **Channel** - every value within each - for a
significant drop in FASR%, a rise in RTO% (Overall), or a rise in NDR%,
comparing the current selected date range against the immediately
preceding period of the same length (the same window the KPI card deltas
use).

- **Severity**: High / Medium / Low, based on how big the change is
  (FASR drops: 10pp/5pp/2pp thresholds; RTO or NDR rises: 6pp/3pp/1.5pp
  and 8pp/4pp/2pp respectively). These thresholds are hardcoded in
  `pipeline.py` (`SEVERITY_THRESHOLDS`) - adjust them there if they don't
  match your risk tolerance.
- **Minimum orders slider** - segments with fewer than this many orders in
  *either* period are skipped, so a courier or state with only a handful
  of orders doesn't get flagged just from small-sample noise.
- Each row shows the worst-severity metric for that segment (a segment
  could be slipping on more than one metric, but only the worst one is
  shown, to keep the list scannable).
- **Caveat worth remembering**: if your date range includes the most
  recent day or two, some flagged dips may just be the usual
  incomplete-cohort effect (recent orders haven't fully resolved yet),
  not a real operational issue - check the order counts and how mature
  those dates actually are before treating something as an actionable
  problem.
- Downloadable as CSV, same as every other table.

### Filters (sidebar, shared by all three tabs)

**Table view - Daily / Monthly**: switches every cut table's columns
(Overall, Payment Cut, Delivery Cut, and the same in Attempt Analysis)
between one column per day and one column per month (e.g. "Jul 2026").
To see a single month's daily breakdown, set the date range to just that
month and stay on Daily; to compare month over month, widen the date
range and switch to Monthly. The KPI cards and the trend/volume chart at
the top stay daily regardless of this toggle.

Date range, plus multi-select filters for Channel, Payment mode (COD/PPD),
State, and Status (Final Status2 - e.g. Delivered, RTO, NDR, In Transit,
CANCELLED). All apply to every KPI, chart, and table on both tabs.
CANCELLED rows are no longer dropped during cleaning - they're kept in the
store and controlled entirely through this Status filter (defaults to
everything selected, i.e. unfiltered).

### Upload history (sidebar)

Every file ever merged in, with a timestamp and row counts, so you can
confirm the store is accumulating correctly across sessions.

## Daily use

1. Open the dashboard (`streamlit run app.py` if it's not already running).
2. In the sidebar, upload the day's raw CSV export(s) - several at once
   is fine.
3. Each file is cleaned (CANCELLED rows dropped, duplicate SKU-level rows
   collapsed to one row per order) and merged into the running history:
   order codes you've already seen are updated in place, new ones are added.
4. Adjust the date range / channel filters as needed - everything on the
   page reacts to them.

## How persistence works (and why removing a file from the uploader is safe)

The merged history lives in `data/raw_store.parquet` on disk, completely
separate from the file uploader widget. Uploading a new file adds to that
history; removing a file from the uploader (or restarting the app) does
**not** touch it. So if you upload today's file, and later upload tomorrow's
file too, both are merged in - and if you then remove either file from the
uploader, previously merged data stays exactly as it was. The Upload History
panel (sidebar) is there specifically so you can verify this for yourself at
any time.

## Resetting

Click "Reset all stored data" in the sidebar to wipe the history (including
the upload log) and start over, or delete `data/raw_store.parquet` and
`data/upload_log.csv` directly. If you reset, upload `seed_original_raw.csv`
again first before your daily exports.

## Notes

- Tables render as one merged block per group (COD/PPD, or each courier)
  with a shared label spanning its rows, sticky header row and label
  columns, and color-coded metric rows (FASR green, RTO red, NDR amber) -
  custom HTML/CSS (`render.py`), not Streamlit's default flat grid.
- The State filter reflects a destination-state column that only exists in
  the regular raw export, not in the original workbook - orders that came
  in via `seed_original_raw.csv` show up as State = "Unknown". The same is
  true for Delivered_date - seed-era orders simply don't have one, so they
  can't contribute to Average Delivery Days (they're still counted normally
  everywhere else).
- The most recent day or two will typically look worse than reality - that
  cohort is still incomplete. This resolves itself as more data comes in;
  it isn't a bug. The KPI deltas can help spot this (a sudden FASR drop on
  the last day or two, with a smaller order count, is usually this effect).
- Expect the file uploader to take a little while on large exports
  (a ~450MB / 1.2M-row file takes roughly 15-20 seconds to clean and merge).
