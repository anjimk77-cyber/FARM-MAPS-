"""
Sri Lanka Shrimp Farm Map - Streamlit App
-------------------------------------------
Pulls farm/customer locations from one Google Sheet, and automatically
computes Last Feed Purchase Date / Due date last Purchase / Last Order
from a separate sales-log Google Sheet (same logic as the Feed Purchase
Report app: Item No. starting with "FEED", excluding returns).

NEW: also pulls pond records from the WaterQualityData Google Sheet and
renders that farm's Pond Layout — one box per pond, colored by status,
showing DOC Today or Full-Harvest info — right inside each marker's
popup, below the existing feed-purchase details. Both the data-loading
and the pond-status logic (Running / Partial H / Full H / Soon to be,
DOC Today, "2nd harvest slot wins", etc.) are ported straight from the
manager app's "Pond Layout" section, so the two apps stay consistent —
including HOW the data is fetched: WaterQualityData is a private sheet,
so (like the manager app) this reads it via gspread + a Google service
account, not a public CSV link.

FIX: the Location column can contain either a plain "lat, lon" string
OR a WKT polygon string like:
    Polygon ((79.7936916 7.552099, 79.7937131 7.5515672, ...))
The old parser only understood "lat, lon" and silently dropped any farm
whose Location was a polygon. parse_location() now handles both: for a
polygon it computes the centroid (for marker placement) AND returns the
ring itself so the farm boundary can be drawn on the map. The boundary
is clickable and shares the exact same popup as the marker.

NEW: also pulls saved user locations (Name / Latitude / Longitude /
Last Updated) from the "UserLocations" worksheet — the same one written
to by the standalone user_location_app.py "Save Location" app — and
plots one marker per user, with their name and last-updated date in the
popup. This uses the exact same private-sheet spreadsheet/credentials
as the Pond Layout feature above (gspread + service account), just a
different worksheet tab. This section is purely additive: it does not
change any farm-location, feed-report, or pond-layout logic above.

Local run:
    pip install -r requirements.txt
    streamlit run app.py
    Needs the same `.streamlit/secrets.toml` as the manager app — the
    `[gcp_service_account]` section, plus a `[gsheet]` section with
    `sheet_id` (the WaterQualityData spreadsheet's key) and optionally
    `worksheet_name` (defaults to "WaterQualityData").

Deploy:
    Push this folder to a GitHub repo, then deploy on
    https://share.streamlit.io (Streamlit Community Cloud), pointing it
    at app.py. The Locations and Sales sheets must be shared as "Anyone
    with the link -> Viewer" (read via plain CSV export). The
    WaterQualityData sheet does NOT need to be public — instead, share it
    with the service account's email (same account/secrets used by the
    manager app) as Viewer or Editor, and set `[gcp_service_account]` /
    `[gsheet]` in this app's Streamlit Cloud secrets.
"""

import re
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
import streamlit.components.v1 as components
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIG
# ============================================================
# Farm/customer location sheet (Customer ID, Customer Name, Farm Name, Location)
LOCATIONS_SHEET_ID = "1v2qTD5iUtdjFTixt9VZ1vM0dZPnyEVz4AYHtILVJi0A"
LOCATIONS_GID = "0"
LOCATIONS_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{LOCATIONS_SHEET_ID}"
    f"/export?format=csv&gid={LOCATIONS_GID}"
)

# Sales log sheet (Date, Customer Code, Item No., Item Description, Quantity, ...)
# — same sheet/logic used by the Feed Purchase Report app.
SALES_SHEET_ID = "1S3csAE-E_hN8vstuHR0KkeAN7yCVQTFe4AkEVlw4vQw"
SALES_GID = "0"
SALES_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SALES_SHEET_ID}"
    f"/export?format=csv&gid={SALES_GID}"
)

# WaterQualityData sheet (Customer, Farm Name with Code, Pond Number, Date,
# Species Culture, Cycle Type, DOC, Harvest Date/Type, Harvest Date 2/Type
# 2, Deleted, Harvest Status, ...) — the SAME sheet + same access method
# (gspread + service account) as the manager app, via
# st.secrets["gcp_service_account"] and st.secrets["gsheet"]["sheet_id"].
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
WATERQUALITY_WORKSHEET_NAME_DEFAULT = "WaterQualityData"

# NEW — saved user locations, written by the separate "Save Location" app
# (user_location_app.py) into a worksheet tab in this SAME spreadsheet.
USERLOC_WORKSHEET_NAME = "UserLocations"

# NEW — optional per-person profile image shown on the map instead of the
# generic person icon. Add an entry here for each name you want a custom
# photo for: "Exact Name As Saved": "direct image URL" (must end in
# something like .jpg/.png, or be a direct-view link, e.g. a Google Drive
# share link converted to "https://drive.google.com/uc?export=view&id=...").
# Any name NOT listed here still falls back to the small red circular
# person icon, so this is fully optional per user.
USER_ICON_URLS = {
    "Anjitha": "https://img.magnific.com/free-photo/young-bearded-man-with-striped-shirt_273609-5677.jpg",
    "Nethusha": "https://img.magnific.com/free-photo/young-bearded-man-with-striped-shirt_273609-5677.jpg",
}

FEED_PREFIX = "FEED"  # Item No. prefix that identifies "feed" items

st.set_page_config(page_title="Farm Map", page_icon="🦐", layout="wide")
st.title("🦐 Farm Locations - Feed Purchase Tracker")


# ============================================================
# DATA LOADING
# ============================================================
@st.cache_data(ttl=300, show_spinner="Loading farm locations...")
def load_locations(url: str) -> pd.DataFrame:
    df = pd.read_csv(url)
    df.columns = [c.strip() for c in df.columns]
    return df


@st.cache_data(ttl=300, show_spinner="Loading sales data...")
def load_sales_data(url: str) -> pd.DataFrame:
    df = pd.read_csv(url)
    df.columns = [c.strip() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Customer Code"] = df["Customer Code"].astype(str).str.strip()
    df["Item No."] = df["Item No."].astype(str).str.strip()
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)
    return df


def _gsheet_configured():
    return "gcp_service_account" in st.secrets and "gsheet" in st.secrets and "sheet_id" in st.secrets["gsheet"]


@st.cache_resource(show_spinner=False)
def get_pond_worksheet():
    """Same pattern as the manager app's get_worksheet(): authorize with
    the service account, open the spreadsheet by key, grab the
    WaterQualityData tab (name overridable via
    st.secrets["gsheet"]["worksheet_name"])."""
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = st.secrets["gsheet"]["sheet_id"]
    worksheet_name = st.secrets["gsheet"].get("worksheet_name", WATERQUALITY_WORKSHEET_NAME_DEFAULT)
    sh = client.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)


@st.cache_data(ttl=300, show_spinner="Loading pond data...")
def load_pond_data() -> pd.DataFrame:
    """Pond records for the Pond Layout popup, read the same way the
    manager app's load_data() reads them: soft-deleted rows
    (Deleted = Yes) and recycle-binned harvest rows
    (Harvest Status = 'H') are dropped."""
    ws = get_pond_worksheet()
    records = ws.get_all_records()
    df = pd.DataFrame(records)
    df.columns = [c.strip() for c in df.columns]

    required_cols = [
        "Customer", "Farm Name with Code", "Pond Number", "Date",
        "Species Culture", "Cycle Type", "DOC",
        "Harvest Date", "Harvest Type", "Harvest Date 2", "Harvest Type 2",
    ]
    for c in required_cols:
        if c not in df.columns:
            df[c] = ""

    if "Deleted" in df.columns:
        is_deleted = df["Deleted"].astype(str).str.strip().str.lower().isin(["yes", "true", "1"])
        df = df[~is_deleted]
    if "Harvest Status" in df.columns:
        is_harvest_hidden = df["Harvest Status"].astype(str).str.strip().str.upper() == "H"
        df = df[~is_harvest_hidden]

    # gspread returns numeric-looking cells as ints/floats and everything
    # else as str, so this column can end up with a mixed dtype — which
    # crashes pandas' sort/groupby (Pond Number is sorted/grouped on below
    # in build_pond_layout_html). Force it to a consistent string type here.
    df["Pond Number"] = df["Pond Number"].astype(str).str.strip()

    return df.reset_index(drop=True)


# ============================================================
# NEW — saved user locations (Name / Latitude / Longitude / Last Updated)
# Read-only here: this app never writes to this tab, only displays it.
# Purely additive — does not affect any function or data above.
# ============================================================
@st.cache_resource(show_spinner=False)
def get_userloc_worksheet():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = st.secrets["gsheet"]["sheet_id"]
    sh = client.open_by_key(sheet_id)
    return sh.worksheet(USERLOC_WORKSHEET_NAME)


@st.cache_data(ttl=60, show_spinner="Loading saved user locations...")
def load_user_locations() -> pd.DataFrame:
    ws = get_userloc_worksheet()
    records = ws.get_all_records()
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["Name", "Latitude", "Longitude", "Last Updated"])
    df.columns = [c.strip() for c in df.columns]
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    return df.dropna(subset=["Latitude", "Longitude"])


def parse_location(location: str):
    """
    Parses the Location cell in either of two formats:
      - "lat, lon"                                    -> plain point
      - "Polygon ((lon lat, lon lat, ...))"            -> WKT polygon ring

    Returns (lat, lon, polygon_points):
      - lat, lon: the point (or polygon centroid) to place the marker/
        badge at, or (None, None) if the value can't be parsed at all.
      - polygon_points: list of (lat, lon) tuples for the ring, in the
        order given, if the value was a WKT polygon; otherwise None.
    """
    if not isinstance(location, str):
        return None, None, None
    location = location.strip()

    if location.lower().startswith("polygon"):
        coords_match = re.search(r"\(\(([^)]+)\)\)", location)
        if not coords_match:
            return None, None, None

        points = []  # (lat, lon) — WKT gives "lon lat", so we swap
        for pair in coords_match.group(1).split(","):
            parts = pair.strip().split()
            if len(parts) != 2:
                continue
            try:
                lon, lat = float(parts[0]), float(parts[1])
                points.append((lat, lon))
            except ValueError:
                continue

        if not points:
            return None, None, None

        avg_lat = sum(p[0] for p in points) / len(points)
        avg_lon = sum(p[1] for p in points) / len(points)
        return avg_lat, avg_lon, points

    match = re.match(r"\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*", location)
    if not match:
        return None, None, None
    return float(match.group(1)), float(match.group(2)), None


def due_color(days):
    """Color-code the badge by urgency of the next feed purchase."""
    try:
        d = float(days)
    except (TypeError, ValueError):
        return "gray"
    if d <= 3:
        return "red"
    elif d <= 7:
        return "orange"
    else:
        return "green"


def build_feed_report(sales: pd.DataFrame) -> pd.DataFrame:
    """
    Same logic as the Feed Purchase Report app's build_report():
    Item No. starts with FEED, excludes returns (Quantity <= 0),
    computes Last Feed Purchase Date, Due date last Purchase (days since),
    and Last Order (items bought on that last purchase date).
    """
    feed_sales = sales[
        sales["Item No."].str.upper().str.startswith(FEED_PREFIX) & (sales["Quantity"] > 0)
    ].copy()

    last_feed = feed_sales.groupby("Customer Code")["Date"].max().rename("Last Feed Purchase Date")
    report = last_feed.reset_index()

    today = pd.Timestamp.now().normalize()
    report["Due date last Purchase"] = (today - report["Last Feed Purchase Date"]).dt.days

    merged = feed_sales.merge(
        report[["Customer Code", "Last Feed Purchase Date"]], on="Customer Code", how="inner"
    )
    same_day = merged[merged["Date"] == merged["Last Feed Purchase Date"]]

    def combine_items(rows: pd.DataFrame) -> str:
        parts = [f"{desc} ({qty:g})" for desc, qty in zip(rows["Item Description"], rows["Quantity"])]
        return ", ".join(parts)

    last_order = same_day.groupby("Customer Code").apply(combine_items).rename("Last Order")
    report = report.merge(last_order, on="Customer Code", how="left")

    report["Last Feed Purchase Date"] = report["Last Feed Purchase Date"].dt.strftime("%Y-%m-%d")
    return report


# ============================================================
# POND LAYOUT (ported from the manager app's Pond Layout section)
# ============================================================
def _escape_html_pond(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _compute_doc_today(row):
    """Same rule as the manager app: saved DOC + days elapsed since Date,
    frozen at the Full-Harvest date once a pond reaches Full H, and stuck
    at 0 for 'Soon to be' ponds that haven't started yet."""
    if str(row.get("Cycle Type") or "").strip() == "Soon to be":
        return "0"
    parsed = pd.to_datetime(row.get("Date"), errors="coerce")
    if pd.isna(parsed):
        return ""
    try:
        doc_num = int(float(row.get("DOC")))
    except (TypeError, ValueError):
        return ""
    t2 = str(row.get("Harvest Type 2", "")).strip().lower()
    t1 = str(row.get("Harvest Type", "")).strip().lower()
    full_harvest_date_str = ""
    if "full" in t2:
        full_harvest_date_str = str(row.get("Harvest Date 2", "")).strip()
    elif "full" in t1:
        full_harvest_date_str = str(row.get("Harvest Date", "")).strip()
    if full_harvest_date_str:
        full_harvest_date = pd.to_datetime(full_harvest_date_str, errors="coerce")
        if pd.notna(full_harvest_date):
            return str(doc_num + (full_harvest_date - parsed).days)
    days_passed = (pd.Timestamp(date.today()) - parsed).days
    return str(doc_num + days_passed)


def _pond_harvest_type(prow):
    return str(prow.get("Harvest Type 2", "")).strip() or str(prow.get("Harvest Type", "")).strip()


def _pond_status(prow, has_partial_history):
    h_type_lower = _pond_harvest_type(prow).lower()
    if "full" in h_type_lower:
        return "Full H"
    elif "partial" in h_type_lower or has_partial_history:
        return "Partial H"
    elif str(prow.get("Cycle Type", "")).strip() == "Soon to be":
        return "Soon to be"
    else:
        return "Running"


def _pond_box_color(status):
    return {
        "Partial H": "#fff3cd",  # yellow
        "Full H": "#d4edda",     # green
        "Soon to be": "#e2e2e2", # gray
    }.get(status, "#eaf4ff")     # default blue — running, no harvest yet


def _species_letter(species):
    s = str(species).strip().lower()
    if "vannamei" in s:
        return "V"
    elif "monodon" in s:
        return "M"
    return ""


def match_pond_rows(pond_df: pd.DataFrame, customer_name: str, farm_name: str) -> pd.DataFrame:
    """Matches the locations sheet's Customer Name / Farm Name to the
    WaterQualityData sheet's Customer / Farm Name with Code columns —
    same fields the manager app filters on. Customer is matched exactly
    (case-insensitive, trimmed); farm is matched as a substring, since
    'Farm Name with Code' usually has an extra code appended after the
    plain farm name."""
    if pond_df.empty:
        return pond_df
    cust = str(customer_name).strip().lower()
    farm = str(farm_name).strip().lower()
    mask_cust = pond_df["Customer"].astype(str).str.strip().str.lower() == cust
    if not farm:
        return pond_df[mask_cust]
    mask_farm = pond_df["Farm Name with Code"].astype(str).str.strip().str.lower().str.contains(
        re.escape(farm), na=False
    )
    return pond_df[mask_cust & mask_farm]


def build_pond_layout_html(farm_pond_df: pd.DataFrame) -> str:
    """Builds the same style of pond-box grid as the manager app's Pond
    Layout section, from that farm's rows in the WaterQualityData sheet."""
    if farm_pond_df.empty:
        return "<div style='font-size:0.8rem;color:#777;'>No pond records found for this farm.</div>"

    df = farm_pond_df.copy()
    df["_ParsedDate"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Pond Number"] = df["Pond Number"].astype(str).str.strip()

    # A pond keeps showing Partial H if ANY of its saved records ever had
    # a Partial harvest — not just its most recent row.
    partial_history_by_pond = (
        df.assign(
            _HasPartial=(
                df["Harvest Type"].astype(str).str.lower().str.contains("partial")
                | df["Harvest Type 2"].astype(str).str.lower().str.contains("partial")
            )
        )
        .groupby("Pond Number")["_HasPartial"]
        .any()
    )

    pond_latest = (
        df.dropna(subset=["_ParsedDate"])
        .sort_values("_ParsedDate")
        .groupby("Pond Number", as_index=False)
        .last()
    )

    # Natural sort (Pond 2 before Pond 10) without relying on raw
    # comparison of the Pond Number text — leading digits sort
    # numerically, ties/non-numeric labels sort alphabetically after.
    def _pond_number_sort_key(v):
        s = str(v)
        m = re.match(r"^(\d+)", s)
        return (0, int(m.group(1)), s) if m else (1, 0, s)

    pond_latest["_SortA"] = pond_latest["Pond Number"].map(lambda v: _pond_number_sort_key(v)[0])
    pond_latest["_SortB"] = pond_latest["Pond Number"].map(lambda v: _pond_number_sort_key(v)[1])
    pond_latest["_SortC"] = pond_latest["Pond Number"].map(lambda v: _pond_number_sort_key(v)[2])
    pond_latest = pond_latest.sort_values(["_SortA", "_SortB", "_SortC"]).drop(
        columns=["_SortA", "_SortB", "_SortC"]
    )

    if pond_latest.empty:
        return "<div style='font-size:0.8rem;color:#777;'>No dated pond records found for this farm.</div>"

    pond_latest["DOC Today"] = pond_latest.apply(_compute_doc_today, axis=1)

    boxes_html = ""
    for _, prow in pond_latest.iterrows():
        pond_no = _escape_html_pond(prow.get("Pond Number", ""))
        has_partial = bool(partial_history_by_pond.get(prow.get("Pond Number", ""), False))
        status = _pond_status(prow, has_partial)
        box_color = _pond_box_color(status)

        if status == "Full H":
            h_date = str(prow.get("Harvest Date 2", "")).strip() or str(prow.get("Harvest Date", "")).strip()
            h_date = _escape_html_pond(h_date or "-")
            middle_html = (
                "<div style='font-size:1.1rem;font-weight:bold;color:red;'>Full H</div>"
                f"<div style='font-size:0.7rem;color:#333;'>{h_date}</div>"
            )
        elif status == "Soon to be":
            middle_html = "<div style='font-size:1rem;font-weight:bold;color:#555;'>Soon to be</div>"
        else:
            doc_today_raw = prow.get("DOC Today", "")
            doc_today_val = _escape_html_pond(doc_today_raw or "-")
            try:
                started_date = (
                    pd.Timestamp(date.today()) - pd.Timedelta(days=int(float(doc_today_raw)))
                ).strftime("%Y-%m-%d")
                started_label = f"Started {started_date}"
            except (TypeError, ValueError):
                started_label = "Started ---"
            middle_html = (
                f"<div style='font-size:1.3rem;font-weight:bold;color:red;'>{doc_today_val}</div>"
                f"<div style='font-size:0.65rem;color:#777;'>{_escape_html_pond(started_label)}</div>"
            )

        species_label = _species_letter(prow.get("Species Culture", ""))
        species_html = (
            f"<div style='font-size:0.7rem;font-weight:bold;color:#444;margin-top:2px;'>{species_label}</div>"
            if species_label else ""
        )

        boxes_html += (
            "<div style='display:inline-flex;flex-direction:column;align-items:center;margin:4px;'>"
            "<div style='width:100px;height:70px;border:2px solid #333;border-radius:6px;"
            "display:flex;flex-direction:column;align-items:center;justify-content:center;"
            f"background:{box_color};'>"
            f"<div style='font-size:0.7rem;color:#555;'>Pond {pond_no}</div>"
            f"{middle_html}"
            "</div>"
            f"{species_html}"
            "</div>"
        )

    legend_html = (
        "<div style='display:flex;gap:10px;flex-wrap:wrap;font-size:0.7rem;margin-bottom:4px;'>"
        "<div><span style='display:inline-block;width:10px;height:10px;background:#eaf4ff;"
        "border:1px solid #333;border-radius:2px;vertical-align:middle;margin-right:3px;'></span>Running</div>"
        "<div><span style='display:inline-block;width:10px;height:10px;background:#fff3cd;"
        "border:1px solid #333;border-radius:2px;vertical-align:middle;margin-right:3px;'></span>Partial H</div>"
        "<div><span style='display:inline-block;width:10px;height:10px;background:#d4edda;"
        "border:1px solid #333;border-radius:2px;vertical-align:middle;margin-right:3px;'></span>Full H</div>"
        "</div>"
    )

    return (
        "<div style='margin-top:6px;'>"
        "<b>Pond Layout</b>"
        f"{legend_html}"
        f"<div style='display:flex;flex-wrap:wrap;max-height:220px;overflow-y:auto;'>{boxes_html}</div>"
        "</div>"
    )


with st.spinner("Loading data..."):
    try:
        raw_locations = load_locations(LOCATIONS_CSV_URL)
        sales_df = load_sales_data(SALES_CSV_URL)
    except Exception as e:
        st.error(
            "Could not load one of the Google Sheets. Make sure both are shared as "
            "'Anyone with the link — Viewer'.\n\n"
            f"Details: {e}"
        )
        st.stop()

# Pond data is loaded separately (via gspread + service account, since
# WaterQualityData is private) and failures here are non-fatal — the map
# and feed-purchase details still work even if secrets aren't configured
# yet, just without the Pond Layout section in the popups.
pond_df = pd.DataFrame()
pond_load_error = None
if _gsheet_configured():
    try:
        pond_df = load_pond_data()
    except Exception as e:
        pond_load_error = str(e)
else:
    pond_load_error = "not_configured"

# NEW — saved user locations, loaded the same non-fatal way as pond data.
# If the "UserLocations" tab doesn't exist yet (nobody has saved a
# location with user_location_app.py yet), this is treated the same as
# "no user locations to show" rather than an error.
user_loc_df = pd.DataFrame()
user_loc_load_error = None
if _gsheet_configured():
    try:
        user_loc_df = load_user_locations()
    except gspread.exceptions.WorksheetNotFound:
        user_loc_df = pd.DataFrame()
    except Exception as e:
        user_loc_load_error = str(e)

if st.sidebar.button("🔄 Refresh data now"):
    load_locations.clear()
    load_sales_data.clear()
    load_pond_data.clear()
    load_user_locations.clear()
    st.rerun()

if pond_load_error == "not_configured":
    st.sidebar.warning(
        "⚠️ Pond Layout unavailable — add the same `[gcp_service_account]` "
        "and `[gsheet]` (with `sheet_id` for the WaterQualityData "
        "spreadsheet) sections used by the manager app to this app's "
        "`.streamlit/secrets.toml`."
    )
elif pond_load_error:
    st.sidebar.warning(
        f"⚠️ Pond Layout unavailable — could not load the WaterQualityData "
        f"sheet. Check your `[gsheet]` secrets and that the sheet is shared "
        f"with the service account.\n\nDetails: {pond_load_error}"
    )

if user_loc_load_error:
    st.sidebar.warning(
        f"⚠️ Saved user locations unavailable — could not load the "
        f"UserLocations sheet.\n\nDetails: {user_loc_load_error}"
    )

# ============================================================
# CLEAN / PREPARE DATA
# ============================================================
df = raw_locations.copy()

parsed_locations = df["Location"].apply(parse_location)
df["lat"] = parsed_locations.apply(lambda x: x[0])
df["lon"] = parsed_locations.apply(lambda x: x[1])
df["polygon"] = parsed_locations.apply(lambda x: x[2])  # list[(lat, lon)] or None

df = df.dropna(subset=["lat", "lon"])

# ============================================================
# PROVINCE FILTER — only show farms in North Western Province or
# Eastern Province. The locations sheet has no province column, so this
# is an approximate bounding-box filter based on each province's rough
# lat/lon extent (Puttalam + Kurunegala districts for North Western;
# Trincomalee + Batticaloa + Ampara districts for Eastern). Adjust
# PROVINCE_BOUNDS below if a farm near a province edge is ever wrongly
# included or excluded.
PROVINCE_BOUNDS = {
    "North Western": {"lat": (7.0, 8.9), "lon": (79.6, 80.6)},
    "Eastern": {"lat": (6.0, 9.1), "lon": (81.0, 82.1)},
}


def _in_allowed_province(lat, lon):
    for bounds in PROVINCE_BOUNDS.values():
        lat_min, lat_max = bounds["lat"]
        lon_min, lon_max = bounds["lon"]
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return True
    return False


df = df[df.apply(lambda r: _in_allowed_province(r["lat"], r["lon"]), axis=1)].reset_index(drop=True)

# Treat "-" or blank farm names as missing
df["Farm Name"] = df["Farm Name"].astype(str).str.strip()
df.loc[df["Farm Name"].isin(["-", "nan", ""]), "Farm Name"] = ""

df["Customer ID"] = df["Customer ID"].astype(str).str.strip()

# Debug panel — since "No pond records found" can come from a Customer
# Name / Farm Name mismatch that's hard to guess blind, this shows the
# actual values being compared side by side. Safe to remove once matching
# is confirmed working.
with st.sidebar.expander("🔧 Pond match debug"):
    st.caption(f"Pond rows loaded: {len(pond_df)}")
    if not pond_df.empty:
        st.write("From WaterQualityData sheet:")
        st.dataframe(
            pond_df[["Customer", "Farm Name with Code"]].drop_duplicates().head(10),
            hide_index=True,
        )
    st.write("From locations sheet:")
    st.dataframe(
        df[["Customer ID", "Customer Name", "Farm Name"]].drop_duplicates().head(10),
        hide_index=True,
    )

# Drop the old static columns from the locations sheet — these now come
# from the sales log automatically instead.
df = df.drop(columns=["Last Feed Purchase Date", "Due date last Purchase"], errors="ignore")

# Compute Last Feed Purchase Date / Due date last Purchase / Last Order
# from the sales sheet, and merge onto each farm by Customer ID <-> Customer Code.
feed_report = build_feed_report(sales_df)
df = df.merge(
    feed_report, left_on="Customer ID", right_on="Customer Code", how="left"
).drop(columns=["Customer Code"], errors="ignore")

df["Due date last Purchase"] = pd.to_numeric(
    df["Due date last Purchase"], errors="coerce"
)

# ============================================================
# SIDEBAR FILTERS
# ============================================================
st.sidebar.header("Filters")

# Customer Name -> Farm Name dropdown search. Farm Name only lists farms
# belonging to whichever customer is currently selected. Nothing on the
# map moves until "Search" is pressed; the last successful search stays
# in effect (via session_state) across reruns until a new one is made.
customer_names = sorted(df["Customer Name"].dropna().astype(str).unique().tolist())
selected_customer = st.sidebar.selectbox(
    "Customer Name", ["-- Select customer --"] + customer_names
)

if selected_customer != "-- Select customer --":
    customer_farm_names = sorted(
        df.loc[df["Customer Name"] == selected_customer, "Farm Name"]
        .replace("", "(none listed)")
        .unique()
        .tolist()
    )
else:
    customer_farm_names = []

selected_farm = st.sidebar.selectbox(
    "Farm Name",
    customer_farm_names if customer_farm_names else ["-- Select customer first --"],
)

search_clicked = st.sidebar.button("🔍 Search")

if "focus_customer" not in st.session_state:
    st.session_state.focus_customer = None
    st.session_state.focus_farm = None

valid_selection = (
    selected_customer != "-- Select customer --"
    and selected_farm not in (None, "-- Select customer first --")
)
if search_clicked and valid_selection:
    st.session_state.focus_customer = selected_customer
    st.session_state.focus_farm = selected_farm

filtered = df.copy()  # always show all farms — search only moves the map

search_matches = pd.DataFrame()
if st.session_state.focus_customer:
    mask = filtered["Customer Name"] == st.session_state.focus_customer
    farm_val = st.session_state.focus_farm
    if farm_val and farm_val != "(none listed)":
        mask &= filtered["Farm Name"] == farm_val
    else:
        mask &= filtered["Farm Name"] == ""
    search_matches = filtered[mask]
    if search_matches.empty:
        st.sidebar.warning("No match found.")
    elif len(search_matches) == 1:
        st.sidebar.success("Zoomed to the selected farm.")
    else:
        st.sidebar.success(
            f"Found {len(search_matches)} matching farms — map zoomed to the first."
        )

st.sidebar.caption(f"Showing {len(filtered)} of {len(df)} farms")

# ============================================================
# BUILD MAP
# ============================================================
if not search_matches.empty:
    focus_df = search_matches.iloc[[0]]  # zoom to the first match only
else:
    focus_df = filtered

if not focus_df.empty:
    center_lat = focus_df["lat"].mean()
    center_lon = focus_df["lon"].mean()
else:
    center_lat, center_lon = 7.8731, 80.7718  # fallback: center of Sri Lanka

# Satellite is now the only map style.
m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles=None)
folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri, Maxar, Earthstar Geographics",
    name="Satellite",
    overlay=False,
    control=False,
).add_to(m)
folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    attr="Esri",
    name="Labels",
    overlay=True,
    control=False,
).add_to(m)

if len(focus_df) > 1:
    bounds = focus_df[["lat", "lon"]].values.tolist()
    m.fit_bounds(bounds, padding=(40, 40))
elif len(focus_df) == 1:
    single_row = focus_df.iloc[0]
    if single_row["polygon"]:
        # This farm's boundary can be much smaller than a flat zoom=17
        # view (e.g. a ~100m pond), which made it effectively invisible
        # — a couple of pixels lost in the basemap. Fit the map to the
        # polygon's own corners instead so it's always clearly framed,
        # regardless of how small or large the actual boundary is.
        m.fit_bounds(single_row["polygon"], padding=(60, 60))
    else:
        m.location = [single_row["lat"], single_row["lon"]]
        m.options["zoom"] = 17

# By default Leaflet renders tooltips above markers, so a nearby farm's
# name label can cover another farm's badge. Swap the stacking order so
# badges always stay on top and stay visible.
m.get_root().html.add_child(folium.Element(
    "<style>.leaflet-tooltip-pane{z-index:600 !important;}"
    ".leaflet-marker-pane{z-index:650 !important;}</style>"
))

for _, row in filtered.iterrows():
    days = row["Due date last Purchase"]
    days_label = "-" if pd.isna(days) else int(days)
    color = due_color(days)

    display_name = (
        f"{row['Customer Name']} — {row['Farm Name']}"
        if row["Farm Name"]
        else row["Customer Name"]
    )

    last_order = row.get("Last Order", "")
    last_order_html = last_order if isinstance(last_order, str) and last_order.strip() else "(no purchase on record)"

    # Pond Layout for this farm, matched from the WaterQualityData sheet
    # by Customer Name + Farm Name, appended below the existing
    # feed-purchase info inside the popup.
    farm_pond_rows = match_pond_rows(pond_df, row["Customer Name"], row["Farm Name"])
    pond_layout_html = build_pond_layout_html(farm_pond_rows)

    popup_html = f"""
        <b>{row['Customer Name']}</b><br>
        Farm: {row['Farm Name'] if row['Farm Name'] else '(none listed)'}<br>
        Customer ID: {row['Customer ID']}<br>
        Last Feed Purchase: {row.get('Last Feed Purchase Date', '-')}<br>
        Due in: {days_label} day(s)<br>
        Last Order: {last_order_html}
        {pond_layout_html}
    """

    badge_html = f"""
        <div style="
            background-color:{color};
            color:white;
            border-radius:50%;
            width:34px;
            height:34px;
            display:flex;
            align-items:center;
            justify-content:center;
            font-weight:bold;
            font-size:13px;
            border:2px solid white;
            box-shadow:0 0 4px rgba(0,0,0,0.4);
        ">{days_label}</div>
    """

    folium.Marker(
        location=[row["lat"], row["lon"]],
        popup=folium.Popup(popup_html, max_width=380),
        tooltip=folium.Tooltip(
            display_name,
            permanent=True,
            direction="bottom",
            offset=(0, 12),
            style=(
                "font-size:13px; font-weight:600; padding:2px 6px; "
                "white-space:nowrap; background:white; "
                "border:1px solid #999; border-radius:4px; "
                "box-shadow:0 1px 3px rgba(0,0,0,0.4); z-index:9999;"
            ),
        ),
        icon=folium.DivIcon(html=badge_html, icon_size=(34, 34), icon_anchor=(17, 17)),
        z_index_offset=1000,
    ).add_to(m)

    # NEW — if this farm's Location was a WKT polygon, draw the actual
    # boundary too. It reuses the exact same popup_html as the marker
    # (each layer needs its own folium.Popup *instance*, but the HTML
    # content is identical) so clicking anywhere on the outline pops up
    # the same feed-purchase + pond-layout info as clicking the badge.
    if row["polygon"]:
        folium.Polygon(
            locations=row["polygon"],
            color="#3388ff",
            weight=3,
            fill=True,
            fill_opacity=0.25,
            popup=folium.Popup(popup_html, max_width=380),
            tooltip=display_name,
        ).add_to(m)

# ============================================================
# NEW — saved user locations, one marker per user (distinct red pin),
# showing their name and last-updated (saved) date in the popup. This
# is purely additive: it runs after all existing farm markers/polygons
# are added above and does not alter any of that logic.
# ============================================================
for _, urow in user_loc_df.iterrows():
    user_name = str(urow.get("Name", "")).strip()
    last_updated = str(urow.get("Last Updated", "")).strip() or "-"

    user_popup_html = f"""
        <b>{user_name}</b><br>
        Saved location<br>
        Last updated: {last_updated}
    """

    # If this name has a custom photo assigned in USER_ICON_URLS, show a
    # small circular badge of that image instead of the generic person
    # icon. Otherwise fall back to the red circle + person icon as before.
    image_url = USER_ICON_URLS.get(user_name)
    if image_url:
        user_badge_html = f"""
            <div style="
                width:32px;
                height:32px;
                border-radius:50%;
                border:2px solid #cc0000;
                box-shadow:0 0 4px rgba(0,0,0,0.4);
                overflow:hidden;
                background:white;
            ">
                <img src="{image_url}" style="width:100%;height:100%;object-fit:cover;" />
            </div>
        """
        icon_size = (32, 32)
        icon_anchor = (16, 16)
    else:
        user_badge_html = """
            <div style="
                background-color:#cc0000;
                color:white;
                border-radius:50%;
                width:26px;
                height:26px;
                display:flex;
                align-items:center;
                justify-content:center;
                font-size:14px;
                border:2px solid white;
                box-shadow:0 0 4px rgba(0,0,0,0.4);
            ">👤</div>
        """
        icon_size = (26, 26)
        icon_anchor = (13, 13)

    folium.Marker(
        location=[urow["Latitude"], urow["Longitude"]],
        popup=folium.Popup(user_popup_html, max_width=260),
        tooltip=folium.Tooltip(
            f"{user_name} (saved {last_updated})",
            permanent=True,
            direction="top",
            offset=(0, -8),
            style=(
                "font-size:12px; font-weight:600; padding:2px 6px; "
                "white-space:nowrap; background:#fff0f0; "
                "border:1px solid #cc0000; border-radius:4px; "
                "box-shadow:0 1px 3px rgba(0,0,0,0.4); z-index:9999;"
            ),
        ),
        icon=folium.DivIcon(html=user_badge_html, icon_size=icon_size, icon_anchor=icon_anchor),
        z_index_offset=1100,
    ).add_to(m)

# st_folium is a *bidirectional* component — even with returned_objects=[],
# Leaflet still reports back to Streamlit on every pan/zoom, and that
# report is what was triggering the full-script rerun (the "running"
# spinner + white flash while everything redraws). The app never reads
# that returned value, so render the map as a plain static HTML embed
# instead: no channel back to Python at all, so pan/zoom is handled
# entirely client-side and can never trigger a Streamlit rerun. Popups,
# tooltips, and the satellite/labels layers all still work exactly the
# same, since those are rendered by Leaflet in the browser either way.
map_html = folium.Figure().add_child(m).render()
components.html(map_html, height=910, width=None)

st.caption(
    ""
    " "
    ""
)
