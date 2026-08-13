# Sri Lanka Farm Map

Interactive Streamlit map that plots customer/farm locations pulled live
from a Google Sheet, with a colored badge showing how many days remain
until the next feed purchase is due.

## 1. Google Sheet setup (one-time)

The app reads your sheet via its public CSV export link, so no API key
or service account is needed — but the sheet must be shared as:

**File → Share → General access → "Anyone with the link" → Viewer**

Your columns should be (header names must match exactly):

```
Customer ID | Customer Name | Farm Name | Location | Last Feed Purchase Date | Due date last Purchase
```

- `Location` must be `latitude, longitude` (e.g. `7.4828224974598525, 79.80804617289405`)
- `Due date last Purchase` should be a number of days (can be negative for overdue)

If your data is on a tab other than the first one, open that tab in the
browser and copy the `gid=...` number from the URL into `SHEET_GID` in
`app.py`.

## 2. Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## 3. Deploy on GitHub + Streamlit Community Cloud

1. Create a new GitHub repo and push this folder:
   ```bash
   git init
   git add .
   git commit -m "Sri Lanka farm map"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
2. Go to https://share.streamlit.io, sign in with GitHub.
3. Click **New app**, pick your repo/branch, set the main file to `app.py`.
4. Click **Deploy**. No secrets are required as long as the sheet is
   shared publicly (view-only) as described above.

## Updating data

Just edit the Google Sheet — the app re-reads it automatically every
5 minutes, or click **"🔄 Refresh data now"** in the sidebar for an
instant update. No redeploy needed.

## Editing the map itself

- `SHEET_ID` / `SHEET_GID` at the top of `app.py` — point it at a
  different sheet/tab.
- `due_color()` — change the day thresholds/colors for the badges.
- Map style dropdown — add more tile layers in the `if map_style == ...`
  block.
