"""
Sri Lanka Shrimp Farm Map - Streamlit App
-------------------------------------------
Pulls farm/customer locations from one Google Sheet, and automatically
computes Last Feed Purchase Date / Due date last Purchase / Last Order
from a separate sales-log Google Sheet (same logic as the Feed Purchase
Report app: Item No. starting with "FEED", excluding returns).

NEW: also pulls pond records from the WaterQualityData Google Sheet (the
same sheet the manager app's "Pond Layout" section uses) and renders that
farm's Pond Layout — one box per pond, colored by status, showing
DOC Today or Full-Harvest info — right inside each marker's popup, below
the existing feed-purchase details. The pond-status logic (Running /
Partial H / Full H / Soon to be, DOC Today, "2nd harvest slot wins", etc.)
is ported from the manager app so both apps stay consistent.

Local run:
    pip install -r requirements.txt
    streamlit run app.py

Deploy:
    Push this folder to a GitHub repo, then deploy on
    https://share.streamlit.io (Streamlit Community Cloud), pointing it
    at app.py. No secrets needed as long as all three Google Sheets below
    are shared as "Anyone with the link -> Viewer" (this app — unlike the
    manager app — reads everything through plain CSV export links, no
    service-account credentials).
"""

import re
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
from datetime import date

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

# NEW — WaterQualityData sheet (Customer, Farm Name with Code, Pond Number,
# Date, Species Culture, Cycle Type, DOC, Harvest Date/Type, Harvest Date
# 2/Type 2, Deleted, Harvest Status, ...). This is the SAME sheet the
# manager app's "Pond Layout" section reads, just pulled here via a plain
# CSV export link instead of gspread + a service account. Fill in your own
# sheet ID / gid below, and make sure that sheet is shared as
# "Anyone with the link -> Viewer" — this app has no Google credentials,
# so a private sheet will fail to load.
WATERQUALITY_SHEET_ID = "1ZRmAb9CymV3o7_D-c9KtzTefg60HvDesLD-TEK2AB2o"
WATERQUALITY_GID = "0"
WATERQUALITY_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{WATERQUALITY_SHEET_ID}"
    f"/export?format=csv&gid={WATERQUALITY_GID}"
)

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


@st.cache_data(ttl=300, show_spinner="Loading pond data...")
def load_pond_data(url: str) -> pd.DataFrame:
    """Pond records for the Pond Layout popup. Mirrors the filtering the
    manager app applies in load_data(): soft-deleted rows (Deleted = Yes)
    and recycle-binned harvest rows (Harvest Status = 'H') are dropped."""
    df = pd.read_csv(url)
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

    # The sheet has no separate 'Customer Code' column — the code (e.g.
    # "C00123") lives at the start of 'Farm Name with Code'. Prefer a
    # regex match for "letter + 5 digits" (handles any separator/spacing
    # after it); fall back to the first 6 non-space characters if that
    # pattern isn't found, so a slightly different code format still gets
    # something to compare against instead of an empty string.
    def _extract_customer_code(v):
        s = str(v).strip()
        m = re.match(r"^([A-Za-z]\s*\d{5})", s)
        if m:
            return re.sub(r"\s+", "", m.group(1)).upper()
        compact = re.sub(r"\s+", "", s)
        return compact[:6].upper()

    df["_DerivedCustomerCode"] = df["Farm Name with Code"].apply(_extract_customer_code)

    return df.reset_index(drop=True)


def parse_lat_lon(location: str):
    """Split a 'lat, lon' string into two floats. Returns (None, None) if invalid."""
    if not isinstance(location, str):
        return None, None
    match = re.match(r"\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*", location)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


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
# NEW — POND LAYOUT (ported from the manager app's Pond Layout section)
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


def match_pond_rows(pond_df: pd.DataFrame, customer_id: str, customer_name: str, farm_name: str) -> pd.DataFrame:
    """Matches a marker to its rows in the WaterQualityData sheet.
    Primary link: Customer ID (locations sheet) <-> the customer code
    embedded in 'Farm Name with Code' (see _extract_customer_code above).
    Both sides are stripped of whitespace and upper-cased before
    comparing, so formatting differences (spaces, case) don't break the
    match. Falls back to the old Customer Name / Farm Name matching only
    if the code doesn't match anything."""
    if pond_df.empty:
        return pond_df

    code = re.sub(r"\s+", "", str(customer_id).strip()).upper()
    if code and "_DerivedCustomerCode" in pond_df.columns:
        code_matches = pond_df[pond_df["_DerivedCustomerCode"] == code]
        if not code_matches.empty:
            return code_matches

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
        .sort_values("Pond Number")
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

# Pond data is loaded separately and failures here are non-fatal — the map
# and feed-purchase details still work even if the WaterQualityData sheet
# isn't reachable yet (e.g. the placeholder sheet ID above hasn't been
# filled in), just without the Pond Layout section in the popups.
pond_df = pd.DataFrame()
pond_load_error = None
try:
    pond_df = load_pond_data(WATERQUALITY_CSV_URL)
except Exception as e:
    pond_load_error = str(e)

if st.sidebar.button("🔄 Refresh data now"):
    load_locations.clear()
    load_sales_data.clear()
    load_pond_data.clear()
    st.rerun()

if pond_load_error:
    st.sidebar.warning(
        "⚠️ Pond Layout unavailable — could not load the WaterQualityData sheet. "
        "Check WATERQUALITY_SHEET_ID/GID at the top of app.py and make sure that "
        "sheet is shared as 'Anyone with the link — Viewer'."
    )

# ============================================================
# CLEAN / PREPARE DATA
# ============================================================
df = raw_locations.copy()

lat_lon = df["Location"].apply(parse_lat_lon)
df["lat"] = lat_lon.apply(lambda x: x[0])
df["lon"] = lat_lon.apply(lambda x: x[1])

df = df.dropna(subset=["lat", "lon"])

# Treat "-" or blank farm names as missing
df["Farm Name"] = df["Farm Name"].astype(str).str.strip()
df.loc[df["Farm Name"].isin(["-", "nan", ""]), "Farm Name"] = ""

df["Customer ID"] = df["Customer ID"].astype(str).str.strip()

# Debug panel — since "No pond records found" can come from a Customer ID
# / derived-code format mismatch that's hard to guess blind, this shows
# the actual values being compared side by side so the mismatch is
# visible directly. Safe to remove once matching is confirmed working.
with st.sidebar.expander("🔧 Pond match debug"):
    st.caption(f"Pond rows loaded: {len(pond_df)}")
    if not pond_df.empty:
        st.write("From WaterQualityData sheet:")
        st.dataframe(
            pond_df[["Customer", "Farm Name with Code", "_DerivedCustomerCode"]].drop_duplicates().head(10),
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

map_style = st.sidebar.selectbox(
    "Map style",
    ["Satellite", "OpenStreetMap", "CartoDB positron", "CartoDB dark_matter"],
)

search = st.sidebar.text_input("Search customer / farm / ID")

filtered = df.copy()  # always show all farms — search only moves the map

search_matches = pd.DataFrame()
if search.strip():
    s = search.strip().lower()
    mask = (
        filtered["Customer Name"].astype(str).str.lower().str.contains(s)
        | filtered["Farm Name"].astype(str).str.lower().str.contains(s)
        | filtered["Customer ID"].astype(str).str.lower().str.contains(s)
    )
    search_matches = filtered[mask]
    if search_matches.empty:
        st.sidebar.warning("No match found.")
    elif len(search_matches) == 1:
        st.sidebar.success("Found 1 match — map zoomed to it.")
    else:
        st.sidebar.success(
            f"Found {len(search_matches)} matches — map zoomed to the first: "
            f"{search_matches.iloc[0]['Customer Name']}."
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

if map_style == "Satellite":
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
else:
    m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles=map_style)

if len(focus_df) > 1:
    bounds = focus_df[["lat", "lon"]].values.tolist()
    m.fit_bounds(bounds, padding=(40, 40))
elif len(focus_df) == 1:
    m.location = [focus_df.iloc[0]["lat"], focus_df.iloc[0]["lon"]]
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

    # NEW — Pond Layout for this farm, matched from the WaterQualityData
    # sheet by Customer Name + Farm Name, appended below the existing
    # feed-purchase info inside the click popup.
    farm_pond_rows = match_pond_rows(pond_df, row["Customer ID"], row["Customer Name"], row["Farm Name"])
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

st_folium(m, width=None, height=900, use_container_width=True)

st.caption(
    ""
    " "
    ""
)
