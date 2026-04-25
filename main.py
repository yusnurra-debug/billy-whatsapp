import os
import json
import re
import base64
import requests
import anthropic
import gspread
from datetime import datetime
from fastapi import FastAPI, Form, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2.service_account import Credentials

app = FastAPI(title="Billy — WhatsApp Expense Tracker")

_anthropic_client = None
_sheet = None
_sheet_headers = None


def get_anthropic():
    global _anthropic_client
    if not _anthropic_client:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def get_sheet():
    global _sheet, _sheet_headers
    if not _sheet:
        creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip("'"))
        creds = Credentials.from_service_account_info(creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        _sheet = gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"]).sheet1
        _sheet_headers = _sheet.row_values(1)
    return _sheet, _sheet_headers


def format_amount(n: float, currency: str = "Rp") -> str:
    n = round(n)
    if n >= 1_000_000:
        return f"{currency} {n / 1_000_000:.1f}jt"
    if n >= 1_000:
        return f"{currency} {n // 1_000}k"
    return f"{currency} {n:,.0f}"


def parse_expense_text(text: str, columns: list) -> dict:
    today = datetime.now().strftime("%d/%m/%Y")
    client = get_anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=f"""You parse WhatsApp expense messages into Google Sheets rows.
Spreadsheet columns: {json.dumps(columns)}
Today: {today}. Currency: Indonesian Rupiah (Rp)
Amount rules: "45k"=45000, "1.5jt"=1500000, "25rb"=25000, "150.000"=150000
Categories: Makanan, Transportasi, Belanja, Tagihan, Hiburan, Kesehatan, Lainnya
Return ONLY raw JSON with column name keys + "_summary": {{"amount": number, "item": "name", "category": "cat"}}""",
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text
    match = re.search(r"\{{[\s\S]*\}}", raw)
    if not match:
        raise ValueError("AI could not parse this expense")
    return json.loads(match.group())


def parse_expense_image(image_url: str, columns: list) -> dict:
    """Download receipt image from Twilio and extract expense using Claude vision."""
    today = datetime.now().strftime("%d/%m/%Y")
    client = get_anthropic()

    # Download image using Twilio credentials
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    img_resp = requests.get(image_url, auth=(sid, token), timeout=15)
    img_resp.raise_for_status()

    content_type = img_resp.headers.get("Content-Type", "image/jpeg")
    img_b64 = base64.standard_b64encode(img_resp.content).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=f"""You extract expense data from receipt/bill photos into Google Sheets rows.
Spreadsheet columns: {json.dumps(columns)}
Today: {today}. Currency: Indonesian Rupiah (Rp)
Look at the receipt image carefully. Extract: total amount, store/item name, category, date (use today if not visible).
Categories: Makanan, Transportasi, Belanja, Tagihan, Hiburan, Kesehatan, Lainnya
Return ONLY raw JSON with column name keys + "_summary": {{"amount": number, "item": "short name", "category": "cat", "store": "store name"}}""",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": content_type,
                        "data": img_b64,
                    },
                },
                {"type": "text", "text": "Extract the expense from this receipt photo."}
            ]
        }],
    )
    raw = response.content[0].text
    match = re.search(r"\{{[\s\S]*\}}", raw)
    if not match:
        raise ValueError("Could not extract expense from image")
    return json.loads(match.group())


@app.get("/")
def health():
    return {"status": "ok", "service": "Billy Expense Tracker"}


@app.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    NumMedia: str = Form(default="0"),
    MediaUrl0: str = Form(default=""),
    MediaContentType0: str = Form(default=""),
):
    body = Body.strip()
    num_media = int(NumMedia)

    # ── Help command ──
    if body.lower() in {"help", "bantuan", "?", "/help", "hi", "hello", "halo"}:
        reply = (
            "*Billy — Expense Assistant* 💰\n\n"
            "Send me your expense in TWO ways:\n\n"
            "📝 *Text:* Just type it\n"
            "• \"lunch warteg 25k\"\n"
            "• \"grab to office 18rb\"\n"
            "• \"bayar listrik 150.000\"\n\n"
            "📸 *Photo:* Send a pic of your receipt/bill\n"
            "I\'ll read it and log it automatically!\n\n"
            "_Type_ help _anytime for this menu._"
        )
        return xml_response(reply)

    # ── Image/receipt ──
    if num_media > 0 and MediaUrl0:
        try:
            sheet, headers = get_sheet()
            parsed = parse_expense_image(MediaUrl0, headers)
            summary = parsed.pop("_summary", {})
            row = [str(parsed.get(col, "")) for col in headers]
            sheet.append_row(row, value_input_option="USER_ENTERED")

            amt = summary.get("amount", 0)
            item = summary.get("item", "Receipt")
            cat = summary.get("category", "")
            store = summary.get("store", "")

            reply = f"📸 *Receipt scanned!*\n✅ {item}"
            if store:
                reply += f" @ {store}"
            reply += f"\n💵 {format_amount(amt)}"
            if cat:
                reply += f"\n📁 {cat}"
            reply += "\n\n_Saved to your Google Sheet!_"
        except Exception as e:
            reply = f"❌ Couldn\'t read receipt: {str(e)[:80]}\n\nTry typing the expense instead, e.g. \"lunch 45k\""
        return xml_response(reply)

    # ── Empty message ──
    if not body:
        return xml_response("Send me an expense or a receipt photo! Type help for examples.")

    # ── Parse text expense ──
    try:
        sheet, headers = get_sheet()
        parsed = parse_expense_text(body, headers)
        summary = parsed.pop("_summary", {})
        row = [str(parsed.get(col, "")) for col in headers]
        sheet.append_row(row, value_input_option="USER_ENTERED")

        amt = summary.get("amount", 0)
        item = summary.get("item", "Expense")
        cat = summary.get("category", "")

        reply = f"✅ *{item}*\n💵 {format_amount(amt)}"
        if cat:
            reply += f"\n📁 {cat}"
        reply += "\n\n_Saved to your Google Sheet!_"
    except gspread.exceptions.APIError as e:
        reply = f"❌ Google Sheets error: {str(e)[:80]}"
    except Exception as e:
        reply = f"❌ {str(e)[:100]}\n\nTry: \"lunch 45k\" or type help"

    return xml_response(reply)


def xml_response(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(content=str(resp), media_type="application/xml")
