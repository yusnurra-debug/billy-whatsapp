import os
import json
import re
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
