"""Static settings: Monday column IDs, labels, and environment variables."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARDS = json.loads((ROOT / "boards.json").read_text())

COUNTRIES = ("USA", "Canada")

MONDAY_ACCOUNT_URL = "https://instastayltds-team.monday.com"

# Column IDs on the monthly expense boards (identical on USA and Canada boards).
COL = {
    "date": "exp_date",
    "amount": "amount",
    "tax": "tax",
    "account": "account",
    "property": "property",
    "category": "expense_category",
    "billable": "billable",
    "report_month": "report_month",
    "receipt": "receipt",
    "receipt_status": "receipt_status",
    "review_status": "review_status",
    "statement_ref": "statement_ref",
    "notes": "notes",
}

# Columns on the expense boards' subitems (one subitem per line on the receipt).
SUB_COL = {"qty": "item_qty", "price": "item_price"}

# "Account" status labels on each country's expense boards.
ACCOUNTS = {
    "USA": ["Amex 1002", "Amex 1007", "Mercury", "Chase 3624", "Checking 7543", "Cash"],
    "Canada": [
        "Amex 1001", "Amex 1007", "Mastercard 9906", "BMO Chequing 3290",
        "BMO Savings 1725", "RBC Savings", "Falis", "Cash",
    ],
}

# "Category" dropdown labels. Utilities is worded differently per country.
_CATEGORIES = [
    "Cleaning", "Laundry & Linens", "Supplies & Consumables", "Repairs & Maintenance",
    "Handyman / Labour", "UTILITIES", "Internet & TV", "Garbage / Snow Removal",
    "Furnishings & Equipment", "Start-Up / Capex", "Software & Subscriptions",
    "Platform & Bank Fees", "Insurance", "Rent / Lease", "Marketing & Photography",
    "Travel & Vehicle", "Meals", "Office & Admin", "Professional Fees",
]
CATEGORIES = {
    "USA": [c.replace("UTILITIES", "Utilities (Electric, Gas, Water)") for c in _CATEGORIES],
    "Canada": [c.replace("UTILITIES", "Utilities (Hydro, Gas, Water)") for c in _CATEGORIES],
}

# Property Master columns used to help Claude match the property name people type
# or the address printed on the receipt.
PROPERTY_NAME_COLUMNS = {
    "USA": ["text_mm84gh2k", "text_mm84fcb0"],       # Hospitable Name, Listing Title
    "Canada": ["text_mm848pmd", "text_mm84qpy8"],
}
PROPERTY_ADDRESS_COLUMNS = {
    "USA": ["text__1", "text_mm84a977"],             # Address, Hospitable Address
    "Canada": ["text__1", "text_mm84qys9"],
}

TIMEZONE = "America/Toronto"
RUN_HOUR_LOCAL = 21

# How far back to look in Slack. Covers missed nights and ⚠️ posts that get fixed later.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

# A statement line counts as the same charge if the date is within this many days.
MATCH_DATE_WINDOW_DAYS = 3

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

DONE_REACTION = "white_check_mark"
WARN_REACTION = "warning"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
