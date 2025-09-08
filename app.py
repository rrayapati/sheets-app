import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

st.title("🍽️ Google Sheets Demo")

# Authenticate using Streamlit secrets
creds = Credentials.from_service_account_info(
    st.secrets["gcp_service_account"],
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)

# Open your Google Sheet (replace with your sheet link)
SHEET_URL = st.secrets["sheets"]["url"]
sh = gc.open_by_url(SHEET_URL)
ws = sh.sheet1

# Display data
rows = ws.get_all_records()
st.write("Current rows:", rows)
