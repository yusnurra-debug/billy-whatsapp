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

app = FastAPI(title="Billy â WhatsApp Expense Tracker")

# Sheet structure: DAY | MONTH | YEAR | MERCHANT | AMOUNT (EUR) | CATEGORY
COLUMNS = ["DAY", "MONTH", "YEAR", "MERCHANT", "AMOUNT (EUR)", "CATEGORY"]
CATEGORIES = [
    "Food", "Grocery", "Cafe", "Shopping",
    "Beauty", "Transport", "Photo", "Culture",
    "Health", "Services", "Digital"
]
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

_anthropic_client = None
_sheet = None


def get_anthropic():
    global _anthropic_client
    if not _anthropic_client:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def get_sheet():
    global _sheet
    if not _sheet:
        creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip("'"))
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        wb = gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"])
        # Find the Data tab (it contains "Data" in the name)
        data_sheet = None
        for ws in wb.worksheets():
            if "data" in ws.title.lower():
                data_sheet = ws
                break
        _sheet = data_sheet or wb.sheet1
    return _sheet


def format_amount(amount: float) -> str:
    return f"EUR{amount:.2f}"


def build_system_prompt() -> str:
    now = datetime.now()
    return f"""You extract expense data and return it as JSON for a spreadsheet with these exact columns:
DAY (number 1-31), MONTH (3-letter: Jan/Feb/Mar/Apr/May/Jun/Jul/Aug/Sep/Oct/Nov/Dec),
YEAR (4 digits), MERCHANT (store/place name), AMOUNT (EUR) (number only, e.g. 12.50), CATEGORY.

Today: {now.day} {MONTH_NAMES[now.month-1]} {now.year}
Currency: EUR (Euro). Convert if needed.

Categories (pick the closest one, text only no emoji):
Food, Grocery, Cafe, Shopping, Beauty, Transport, Photo, Culture, Health, Services, Digital

Return ONLY raw JSON like:
{{"DAY": 18, "MONTH": "Mar", "YEAR": 2026, "MERCHANT": "Aldi Bcn Muntaner", "AMOUNT (EUR)": 12.50, "CATEGORY": "Grocery",
  "_summary": {{"amount": 12.50, "item": "Aldi", "category": "Grocery"}}}}"""


def parse_expense_text(text: str) -> dict:
    client = get_anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("Could not parse expense")
    return json.loads(match.group())


def parse_expense_image(image_url: str) -> dict:
    client = get_anthropic()
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    img_resp = requests.get(image_url, auth=(sid, token), timeout=15)
    img_resp.raise_for_status()
    content_type = img_resp.headers.get("Content-Type", "image/jpeg")
    img_b64 = base64.standard_b64encode(img_resp.content).decode("utf-8")

    client2 = get_anthropic()
    response = client2.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=build_system_prompt() + """

This is a receipt/bill photo. Look carefully at:
- The TOTAL amount (not subtotal)
- The merchant/store name
- The date (use today if not visible)
- Choose the best category based on the type of store""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": img_b64}},
                {"type": "text", "text": "Extract the expense from this receipt."}
            ]
        }],
    )
    raw = response.content[0].text
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("Could not read receipt")
    return json.loads(match.group())


def append_to_sheet(parsed: dict) -> None:
    sheet = get_sheet()
    summary = parsed.pop("_summary", {})
    row = [
        str(parsed.get("DAY", "")),
        str(parsed.get("MONTH", "")),
        str(parsed.get("YEAR", "")),
        str(parsed.get("MERCHANT", "")),
        str(parsed.get("AMOUNT (EUR)", "")),
        str(parsed.get("CATEGORY", "")),
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    parsed["_summary"] = summary


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

    # Help
    if body.lower() in {"help", "?", "/help", "hi", "hello", "halo", "bantuan"}:
        reply = (
            "*Billy â Expense Tracker* ð³\n\n"
            "I log expenses directly to your spreadsheet!\n\n"
            "ð *Text* â type naturally:\n"
            "â¢ \"Aldi 12.50\"\n"
            "â¢ \"Uber Eats 24 food\"\n"
            "â¢ \"Metro 2.40 transport\"\n\n"
            "ð¸ *Photo* â send a pic of your receipt\n"
            "I\'ll read the total and log it automatically!\n\n"
            "_Categories: Food, Grocery, Cafe, Shopping,_\n"
            "_Beauty, Transport, Photo, Culture, Health,_\n"
            "_Services, Digital_"
        )
        return xml_response(reply)

    # Image receipt
    if num_media > 0 and MediaUrl0:
        try:
            parsed = parse_expense_image(MediaUrl0)
            summary = parsed.get("_summary", {})
            append_to_sheet(parsed)
            amt = summary.get("amount", 0)
            item = summary.get("item", "Receipt")
            cat = summary.get("category", "")
            reply = f"ð¸ *Receipt scanned!*\nâ {item}\nð¶ EUR{amt:.2f}"
            if cat:
                reply += f"\nð· {cat}"
            reply += "\n\n_Added to your Data sheet!_ ð"
        except Exception as e:
            reply = f"â Couldn\'t read receipt: {str(e)[:80]}\n\nTry typing it instead, e.g. \"Aldi 12.50 grocery\""
        return xml_response(reply)

    # Empty
    if not body:
        return xml_response("Send me an expense or a receipt photo! Type help for examples. ð³")

    # Text expense
    try:
        parsed = parse_expense_text(body)
        summary = parsed.get("_summary", {})
        append_to_sheet(parsed)
        amt = summary.get("amount", 0)
        item = summary.get("item", "Expense")
        cat = summary.get("category", "")
        day = parsed.get("DAY", "")
        month = parsed.get("MONTH", "")
        reply = f"â *{item}*\nð¶ EUR{amt:.2f}"
        if cat:
            reply += f"\nð· {cat}"
        if day and month:
            reply += f"\nð {day} {month}"
        reply += "\n\n_Added to your Data sheet!_ ð"
    except gspread.exceptions.APIError as e:
        reply = f"â Sheets error: {str(e)[:80]}\n\nMake sure the sheet is shared with the service account."
    except Exception as e:
        reply = f"â {str(e)[:100]}\n\nTry: \"Aldi 12.50\" or type help"

    return xml_response(reply)


def xml_response(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(content=str(resp), media_type="application/xml")
