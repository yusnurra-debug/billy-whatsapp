import os
import json
import re
import anthropic
import gspread
from datetime import datetime
from fastapi import FastAPI, Form, Request, Response
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2.service_account import Credentials

app = FastAPI(title="Billy - WhatsApp Expense Tracker")

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
        raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip("'").strip('"')
        creds_info = json.loads(raw)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
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


def parse_expense(text: str, columns: list) -> dict:
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


@app.get("/")
def health():
    return {"status": "ok", "service": "Billy Expense Tracker"}


@app.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    NumMedia: str = Form(default="0")
):
    body = Body.strip()
    if int(NumMedia) > 0:
        return xml_response("I can only read text. Send your expense like: lunch 45k")
    if body.lower() in {"help", "bantuan", "?", "/help", "hi", "hello", "halo"}:
        reply = "*Billy - Expense Assistant*\n\nSend me what you spent:\n* \"lunch warteg 25k\"\n* \"grab to office 18rb\"\n* \"bayar listrik 150.000\"\n\nI'll log it to your Google Sheet automatically!"
        return xml_response(reply)
    if not body:
        return xml_response("Send me an expense! E.g. \"lunch 45k\"\nType help for examples.")
    try:
        sheet, headers = get_sheet()
        parsed = parse_expense(body, headers)
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
    except Exception as e:
        reply = f"❌ {str(e)[:100]}\n\nTry: \"lunch 45k\" or type help"
    return xml_response(reply)


def xml_response(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(content=str(resp), media_type="application/xml")
