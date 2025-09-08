import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

st.set_page_config(page_title="Coupons & Meals", page_icon="🍽️", layout="centered")
st.title("🍽️ Google Sheets Demo")

# --- Guardrails ---
if "gcp_service_account" not in st.secrets:
    st.error("Secrets missing: add your service-account JSON under [gcp_service_account].")
    st.stop()
if "sheets" not in st.secrets or "url" not in st.secrets["sheets"]:
    st.error('Sheets URL missing: add [sheets]\\nurl="https://docs.google.com/..." in Secrets.')
    st.stop()

# --- Auth ---
creds = Credentials.from_service_account_info(
    st.secrets["gcp_service_account"],
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",  # harmless if not on Shared Drive
    ],
)
gc = gspread.authorize(creds)

# --- Open sheet ---
SHEET_URL = st.secrets["sheets"]["url"]
sh = gc.open_by_url(SHEET_URL)
ws = sh.sheet1  # first worksheet (headers in row 1)

# --- Read with cache ---
@st.cache_data(ttl=30)
def get_rows():
    # Returns list of dicts: [] if only headers exist
    return ws.get_all_records()

rows = get_rows()

st.subheader("Latest entries")
st.dataframe(rows if rows else [], use_container_width=True)
if st.button("Refresh table"):
    st.cache_data.clear()
    rows = get_rows()
    st.success("Refreshed!")

st.divider()
st.subheader("Add an entry")

with st.form("add_entry", clear_on_submit=True):
    user = st.text_input("User name")
    item = st.selectbox("Item", ["Breakfast", "Lunch", "Dinner", "Coupon"])
    qty = st.number_input("Quantity", min_value=1, step=1, value=1)
    notes = st.text_input("Notes (optional)")
    admin_pass = st.text_input("Admin pass", type="password")
    submitted = st.form_submit_button("Submit")

if submitted:
    expected = st.secrets["sheets"].get("admin_pass", "")
    if not expected:
        st.error('Set an admin pass in Secrets under [sheets] admin_pass="..."')
    elif admin_pass != expected:
        st.error("Invalid admin pass.")
    elif not user.strip():
        st.error("User name is required.")
    else:
        ws.append_row([
            datetime.now().isoformat(timespec="seconds"),
            user, item, int(qty), notes
        ])
        st.cache_data.clear()
        st.success("Saved! Click “Refresh table” above to see it.")
