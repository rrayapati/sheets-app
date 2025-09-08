import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
from io import BytesIO
import qrcode
import uuid
import requests

st.set_page_config(page_title="Meals & Coupons", page_icon="🍽️", layout="centered")
st.title("🍽️ Meals & Coupons — Admin")

# -----------------------------
# Guardrails: Secrets & Config
# -----------------------------
if "gcp_service_account" not in st.secrets:
    st.error("Secrets missing: add your service-account JSON under [gcp_service_account].")
    st.stop()
if "sheets" not in st.secrets or "url" not in st.secrets["sheets"]:
    st.error('Sheets URL missing: add [sheets]\\nurl="https://docs.google.com/..." in Secrets.')
    st.stop()

ADMIN_PASS = st.secrets["sheets"].get("admin_pass", "")

# -----------------------------
# Auth to Google Sheets
# -----------------------------
creds = Credentials.from_service_account_info(
    st.secrets["gcp_service_account"],
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
)
gc = gspread.authorize(creds)
SHEET_URL = st.secrets["sheets"]["url"]
sh = gc.open_by_url(SHEET_URL)

# ---- Helpers to get/create sheets
def get_or_create_ws(title: str, rows=1000, cols=20):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))

# ========== BASIC SHEET (Sheet1) ==========
ws_basic = sh.sheet1  # your original sheet (headers: timestamp, user, item, qty, notes)

@st.cache_data(ttl=30)
def get_basic_rows():
    return ws_basic.get_all_records()  # [] if only headers

# ========== TOKEN SHEETS ==========
# tokens: id, user, type, start, end, allowance, used, status, issued_ts, payload
# uses:   ts, token_id, user_scanned, note
ws_tokens = get_or_create_ws("tokens")
ws_uses   = get_or_create_ws("uses")

def ensure_headers(ws, headers):
    cur = ws.row_values(1)
    if cur != headers:
        ws.update("A1", [headers])

ensure_headers(ws_tokens, ["id","user","type","start","end","allowance","used","status","issued_ts","payload"])
ensure_headers(ws_uses,   ["ts","token_id","user_scanned","note"])

@st.cache_data(ttl=20)
def read_tokens():
    recs = ws_tokens.get_all_records()
    for r in recs:
        r["start"] = str(r.get("start",""))
        r["end"]   = str(r.get("end",""))
        r["issued_ts"] = str(r.get("issued_ts",""))
    return recs

@st.cache_data(ttl=20)
def read_uses():
    return ws_uses.get_all_records()

def append_token_row(token):
    ws_tokens.append_row([
        token["id"], token["user"], token["type"],
        token["start"], token["end"], token["allowance"],
        token["used"], token["status"], token["issued_ts"],
        token["payload"]
    ])
    st.cache_data.clear()

def update_token_used(token_id, new_used, new_status=None):
    cells = ws_tokens.findall(token_id, in_column=1)  # col A = id
    if not cells:
        raise ValueError("Token not found")
    row = cells[0].row
    ws_tokens.update_cell(row, 7, new_used)  # used
    if new_status:
        ws_tokens.update_cell(row, 8, new_status)  # status
    st.cache_data.clear()

def append_use_row(ts, token_id, user_scanned, note=""):
    ws_uses.append_row([ts, token_id, user_scanned, note])
    st.cache_data.clear()

# -----------------------------
# Token payload (encoded in QR)
# -----------------------------
# MTK|id=ABCD1234|user=Name|type=Lunch|allow=20|start=YYYY-MM-DD|end=YYYY-MM-DD
def make_payload(token):
    return (
        f"MTK|id={token['id']}"
        f"|user={token['user']}"
        f"|type={token['type']}"
        f"|allow={token['allowance']}"
        f"|start={token['start']}"
        f"|end={token['end']}"
    )

def parse_payload(payload: str):
    if not payload or not payload.startswith("MTK|"):
        return None
    parts = payload.split("|")[1:]
    kv = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k] = v
    for required in ["id","user","type","allow","start","end"]:
        if required not in kv:
            return None
    try:
        allow = int(kv["allow"])
    except:
        return None
    return {
        "id": kv["id"],
        "user": kv["user"],
        "type": kv["type"],
        "allowance": allow,
        "start": kv["start"],
        "end": kv["end"],
        "payload": payload
    }

def within_validity(today_iso: str, start_iso: str, end_iso: str):
    try:
        return (today_iso >= start_iso) and (today_iso <= end_iso)
    except:
        return False

# -----------------------------
# Email & WhatsApp helpers (optional)
# -----------------------------
def send_email_with_png_via_sendgrid(to_email: str, subject: str, body_text: str, png_bytes: bytes, filename: str = "qr.png"):
    key = st.secrets.get("email", {}).get("sendgrid_api_key", "")
    from_email = st.secrets.get("email", {}).get("from_email", "")
    if not key or not from_email:
        return False, "Email not configured (need [email] sendgrid_api_key & from_email in Secrets)."
    import base64
    encoded = base64.b64encode(png_bytes).decode("utf-8")
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body_text}],
        "attachments": [{
            "content": encoded,
            "type": "image/png",
            "filename": filename,
            "disposition": "attachment"
        }]
    }
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    if resp.status_code in (200, 202):
        return True, "Email queued"
    return False, f"SendGrid error {resp.status_code}: {resp.text}"

def whatsapp_upload_media(png_bytes: bytes, filename: str = "qr.png"):
    token = st.secrets.get("whatsapp", {}).get("token", "")
    phone_number_id = st.secrets.get("whatsapp", {}).get("phone_number_id", "")
    if not token or not phone_number_id:
        return False, "WhatsApp not configured", None
    files = {'file': (filename, png_bytes, 'image/png')}
    data = {'messaging_product': 'whatsapp'}
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, files=files, data=data, timeout=30)
    if resp.status_code == 200:
        media_id = resp.json().get("id")
        return True, "Uploaded", media_id
    return False, f"Upload error {resp.status_code}: {resp.text}", None

def whatsapp_send_image(phone_e164: str, media_id: str, caption: str = ""):
    token = st.secrets.get("whatsapp", {}).get("token", "")
    phone_number_id = st.secrets.get("whatsapp", {}).get("phone_number_id", "")
    if not token or not phone_number_id:
        return False, "WhatsApp not configured"
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_e164,
        "type": "image",
        "image": {"id": media_id, "caption": caption}
    }
    resp = requests.post(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }, json=payload, timeout=30)
    if resp.status_code in (200, 201):
        return True, "WhatsApp sent"
    return False, f"Send error {resp.status_code}: {resp.text}"

# =========================================================
# SECTION 1: Latest entries (basic) + Add an entry (Sheet1)
# =========================================================
st.subheader("Latest entries")
basic_rows = get_basic_rows()
st.dataframe(basic_rows if basic_rows else [], use_container_width=True)
if st.button("Refresh table"):
    st.cache_data.clear()
    basic_rows = get_basic_rows()
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
        ws_basic.append_row([
            datetime.now().isoformat(timespec="seconds"),
            user, item, int(qty), notes
        ])
        st.cache_data.clear()
        st.success("Saved! Click “Refresh table” above to see it.")

# =========================================================
# SECTION 2: Tokens — Generate / Validate / Dashboard
# =========================================================
st.divider()
tab1, tab2, tab3 = st.tabs(["➕ Generate QR (one per period)", "✅ Validate / Use", "📊 Tokens Dashboard"])

# ---------- TAB 1: Generate QR token ----------
with tab1:
    st.markdown("Create **one QR** that covers a date range with a total allowance (number of uses).")
    with st.form("gen_form", clear_on_submit=True):
        g_user = st.text_input("User name")
        g_type = st.selectbox("Type", ["Breakfast", "Lunch", "Dinner", "Coupon"])
        colA, colB = st.columns(2)
        g_start = colA.date_input("Start date", value=date.today())
        g_end   = colB.date_input("End date", value=date.today() + timedelta(days=30))
        g_allow = st.number_input("Total allowance (uses)", min_value=1, step=1, value=20)
        g_admin = st.text_input("Admin password", type="password")
        submitted_qr = st.form_submit_button("Generate QR")

    if submitted_qr:
        if not ADMIN_PASS:
            st.error('Set [sheets] admin_pass="your-pass" in Secrets.')
        elif g_admin != ADMIN_PASS:
            st.error("Invalid admin password.")
        elif not g_user.strip():
            st.error("User name is required.")
        elif g_start > g_end:
            st.error("Start date must be on or before end date.")
        else:
            token_id = uuid.uuid4().hex[:8].upper()
            token = {
                "id": token_id,
                "user": g_user.strip(),
                "type": g_type,
                "start": g_start.isoformat(),
                "end": g_end.isoformat(),
                "allowance": int(g_allow),
                "used": 0,
                "status": "ACTIVE",
                "issued_ts": datetime.now().isoformat(timespec="seconds"),
            }
            token["payload"] = make_payload(token)

            append_token_row(token)

            img = qrcode.make(token["payload"])
            buf = BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            st.image(png_bytes, caption=f"QR for {token['user']} — {token['type']}", use_column_width=False)
            st.download_button(
                "Download QR",
                png_bytes,
                file_name=f"{token['user']}_{token['type']}_{token_id}.png",
                mime="image/png"
            )
            st.success("Token created. Share this QR with the user.")

            # ---- Send channels (optional) ----
            st.markdown("### Send QR")
            col1, col2 = st.columns(2)
            with col1:
                with st.form("email_qr_form", clear_on_submit=True):
                    to_email = st.text_input("Recipient email")
                    email_btn = st.form_submit_button("Send Email")
                if email_btn:
                    ok, msg = send_email_with_png_via_sendgrid(
                        to_email=to_email,
                        subject=f"Meal Token QR — {token['user']} ({token['type']})",
                        body_text=(
                            f"Hello {token['user']},\n\n"
                            f"Attached is your QR for {token['type']}.\n"
                            f"Validity: {token['start']} → {token['end']}\n"
                            f"Allowance: {token['allowance']}\n"
                            f"Token ID: {token['id']}\n\n"
                            f"Please keep it safe."
                        ),
                        png_bytes=png_bytes,
                        filename=f"{token['user']}_{token['type']}_{token_id}.png"
                    )
                    (st.success if ok else st.error)(msg)
            with col2:
                with st.form("wa_qr_form", clear_on_submit=True):
                    wa_number = st.text_input("WhatsApp number (E.164, e.g., +9198xxxxxxx)")
                    wa_btn = st.form_submit_button("Send WhatsApp")
                if wa_btn:
                    ok_up, msg_up, media_id = whatsapp_upload_media(png_bytes, filename=f"{token['id']}.png")
                    if not ok_up:
                        st.error(msg_up)
                    else:
                        caption = (f"{token['user']} — {token['type']}\n"
                                   f"Valid: {token['start']} → {token['end']}\n"
                                   f"Allowance: {token['allowance']}  ID: {token['id']}")
                        ok_send, msg_send = whatsapp_send_image(wa_number, media_id, caption=caption)
                        (st.success if ok_send else st.error)(msg_send)

# ---------- TAB 2: Validate / Use ----------
with tab2:
    st.markdown("Scan a QR (or paste its text) to **consume a use** and update counts.")
    with st.form("scan_form", clear_on_submit=True):
        scan_text = st.text_area("Scanned QR text (payload)", placeholder="Paste the decoded QR content here...")
        admin2 = st.text_input("Admin password", type="password")
        use_btn = st.form_submit_button("Validate & Use")

    if use_btn:
        if admin2 != ADMIN_PASS:
            st.error("Invalid admin password.")
        else:
            parsed = parse_payload(scan_text.strip())
            if not parsed:
                st.error("Invalid payload. Expecting MTK|id=...|user=...|type=...|allow=...|start=YYYY-MM-DD|end=YYYY-MM-DD")
            else:
                tokens = read_tokens()
                token = next((t for t in tokens if t["id"] == parsed["id"]), None)
                if not token:
                    st.error("Token not found in 'tokens' sheet.")
                else:
                    today_iso = date.today().isoformat()
                    valid = within_validity(today_iso, token["start"], token["end"])
                    remaining = int(token["allowance"]) - int(token["used"])
                    st.info(
                        f"**User:** {token['user']} | **Type:** {token['type']}  \n"
                        f"**Start:** {token['start']} | **End:** {token['end']}  \n"
                        f"**Allowance:** {token['allowance']} | **Used:** {token['used']} | **Remaining:** {remaining}  \n"
                        f"**Status:** {token['status']}"
                    )
                    if token["status"] != "ACTIVE":
                        st.error("Token is not ACTIVE.")
                    elif not valid:
                        st.error("Token is outside the valid date range.")
                    elif remaining <= 0:
                        st.error("No remaining uses.")
                    else:
                        new_used = int(token["used"]) + 1
                        new_status = "ACTIVE" if new_used < int(token["allowance"]) else "EXHAUSTED"
                        try:
                            update_token_used(token["id"], new_used, new_status)
                            append_use_row(datetime.now().isoformat(timespec="seconds"), token["id"], token["user"], note="scan")
                            st.success("Use recorded ✅")
                        except Exception as e:
                            st.error(f"Failed to update token: {e}")

# ---------- TAB 3: Dashboard ----------
with tab3:
    tokens = read_tokens()
    st.subheader("All Tokens")
    st.dataframe(tokens, use_container_width=True)

    st.divider()
    st.subheader("Inspect a Token")
    ids = [t["id"] for t in tokens] if tokens else []
    sel = st.selectbox("Select token id", ids)
    if sel:
        tok = next(t for t in tokens if t["id"] == sel)
        remaining = int(tok["allowance"]) - int(tok["used"])
        st.markdown(
            f"""
**User:** {tok['user']}  
**Type:** {tok['type']}  
**Start → End:** {tok['start']} → {tok['end']}  
**Allowance:** {tok['allowance']}  
**Used:** {tok['used']}  
**Remaining:** {remaining}  
**Status:** {tok['status']}  
**Issued:** {tok['issued_ts']}  
"""
        )
        with st.expander("Show payload (encoded in the QR)"):
            st.code(tok["payload"], language="text")
        c1, c2, c3 = st.columns(3)
        c1.metric("Allowance", tok["allowance"])
        c2.metric("Used", tok["used"])
        c3.metric("Remaining", remaining)

    st.divider()
    st.subheader("Recent Uses")
    uses = read_uses()
    st.dataframe(uses, use_container_width=True)
