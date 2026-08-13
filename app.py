"""
Sri Lanka Shrimp Farm Map - Streamlit App
-------------------------------------------
Pulls farm/customer locations from one Google Sheet, and automatically
computes Last Feed Purchase Date / Due date last Purchase / Last Order
from a separate sales-log Google Sheet (same logic as the Feed Purchase
Report app: Item No. starting with "FEED", excluding returns).

Local run:
    pip install -r requirements.txt
    streamlit run app.py

Deploy:
    Push this folder to a GitHub repo, then deploy on
    https://share.streamlit.io (Streamlit Community Cloud), pointing it
    at app.py. No secrets needed as long as both Google Sheets are shared
    as "Anyone with the link -> Viewer".
"""

import re
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

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

FEED_PREFIX = "FEED"  # Item No. prefix that identifies "feed" items

st.set_page_config(page_title="Sri Lanka Farm Map", page_icon="🦐", layout="wide")
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

if st.sidebar.button("🔄 Refresh data now"):
    load_locations.clear()
    load_sales_data.clear()
    st.rerun()

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

    popup_html = f"""
        <b>{row['Customer Name']}</b><br>
        Farm: {row['Farm Name'] if row['Farm Name'] else '(none listed)'}<br>
        Customer ID: {row['Customer ID']}<br>
        Last Feed Purchase: {row.get('Last Feed Purchase Date', '-')}<br>
        Due in: {days_label} day(s)<br>
        Last Order: {last_order_html}
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
        popup=folium.Popup(popup_html, max_width=280),
        tooltip=folium.Tooltip(
            display_name,
            permanent=True,
            direction="bottom",
            offset=(0, 12),
            style=(
                "font-size:13px; font-weight:700; padding:2px 4px; "
                "white-space:nowrap; z-index:9999; color:#111; "
                "background:transparent; border:none; box-shadow:none; "
                "text-shadow: -1px -1px 0 #fff, 1px -1px 0 #fff, "
                "-1px 1px 0 #fff, 1px 1px 0 #fff, 0 0 3px #fff;"
            ),
        ),
        icon=folium.DivIcon(html=badge_html, icon_size=(34, 34), icon_anchor=(17, 17)),
        z_index_offset=1000,
    ).add_to(m)

st_folium(m, width=None, height=900, use_container_width=True)

st.caption(
    "Badge = days until next feed purchase is due "
    "(🔴 0-3 days, 🟠 4-7 days, 🟢 8+ days). Click a badge to see Last Order. "
    "Data refreshes from Google Sheets every 5 minutes, or click 'Refresh data now'."
)
