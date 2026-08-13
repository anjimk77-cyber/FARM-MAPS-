"""
Sri Lanka Shrimp Farm Map - Streamlit App
-------------------------------------------
Pulls customer/farm data live from a public Google Sheet and plots each
farm on an interactive (satellite/street) map, with a badge showing days
until/since the feed-purchase due date.

Local run:
    pip install -r requirements.txt
    streamlit run app.py

Deploy:
    Push this folder to a GitHub repo, then deploy on
    https://share.streamlit.io (Streamlit Community Cloud), pointing it
    at app.py. No secrets needed as long as the Google Sheet is shared as
    "Anyone with the link -> Viewer".
"""

import re
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

# ============================================================
# CONFIG
# ============================================================
# Your Google Sheet ID (from the URL) and the tab's gid.
# Default tab gid is usually 0 — change it if your data is on another tab.
SHEET_ID = "1v2qTD5iUtdjFTixt9VZ1vM0dZPnyEVz4AYHtILVJi0A"
SHEET_GID = "0"

CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"

st.set_page_config(page_title="Sri Lanka Farm Map", page_icon="🦐", layout="wide")
st.title("🦐 Farm Locations - Feed Purchase Tracker")


# ============================================================
# DATA LOADING
# ============================================================
@st.cache_data(ttl=300)  # refresh from Google Sheets every 5 minutes
def load_data(url: str) -> pd.DataFrame:
    df = pd.read_csv(url)
    df.columns = [c.strip() for c in df.columns]
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


with st.spinner("Loading data from Google Sheet..."):
    try:
        raw_df = load_data(CSV_URL)
    except Exception as e:
        st.error(
            "Could not load the Google Sheet. Make sure it's shared as "
            "'Anyone with the link — Viewer'.\n\n"
            f"Details: {e}"
        )
        st.stop()

if st.sidebar.button("🔄 Refresh data now"):
    load_data.clear()
    st.rerun()

# ============================================================
# CLEAN / PREPARE DATA
# ============================================================
df = raw_df.copy()

lat_lon = df["Location"].apply(parse_lat_lon)
df["lat"] = lat_lon.apply(lambda x: x[0])
df["lon"] = lat_lon.apply(lambda x: x[1])

df = df.dropna(subset=["lat", "lon"])

# Treat "-" or blank farm names as missing
df["Farm Name"] = df["Farm Name"].astype(str).str.strip()
df.loc[df["Farm Name"].isin(["-", "nan", ""]), "Farm Name"] = ""

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
    else:
        st.sidebar.success(f"Found {len(search_matches)} match(es) — map centered on result.")

# Build a label for each row so it can be picked from a dropdown
def make_label(row):
    return f"{row['Customer Name']} — {row['Farm Name']}" if row["Farm Name"] else row["Customer Name"]

area_options = ["-- All areas --"] + [make_label(r) for _, r in df.iterrows()]
selected_area = st.sidebar.selectbox("Select area", area_options)

selected_match = pd.DataFrame()
if selected_area != "-- All areas --":
    labels = df.apply(make_label, axis=1)
    selected_match = df[labels == selected_area]

st.sidebar.caption(f"Showing {len(filtered)} of {len(df)} farms")

# ============================================================
# BUILD MAP
# ============================================================
if not selected_match.empty:
    focus_df = selected_match
elif not search_matches.empty:
    focus_df = search_matches
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

for _, row in filtered.iterrows():
    days = row["Due date last Purchase"]
    days_label = "-" if pd.isna(days) else int(days)
    color = due_color(days)

    display_name = (
        f"{row['Customer Name']} — {row['Farm Name']}"
        if row["Farm Name"]
        else row["Customer Name"]
    )

    popup_html = f"""
        <b>{row['Customer Name']}</b><br>
        Farm: {row['Farm Name'] if row['Farm Name'] else '(none listed)'}<br>
        Customer ID: {row['Customer ID']}<br>
        Last Feed Purchase: {row.get('Last Feed Purchase Date', '-')}<br>
        Due in: {days_label} day(s)
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
                "font-size:13px; font-weight:600; padding:2px 4px; "
                "white-space:nowrap; z-index:9999;"
            ),
        ),
        icon=folium.DivIcon(html=badge_html, icon_size=(34, 34), icon_anchor=(17, 17)),
        z_index_offset=1000,
    ).add_to(m)

st_folium(m, width=None, height=900, use_container_width=True)

st.caption(
    "Badge = days until next feed purchase is due "
    "(🔴 0-3 days, 🟠 4-7 days, 🟢 8+ days). "
    "Data refreshes from Google Sheets every 5 minutes, or click 'Refresh data now'."
)
