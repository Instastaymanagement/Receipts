"""Static settings: Monday column IDs, labels, and environment variables."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARDS = json.loads((ROOT / "boards.json").read_text())
# Team nicknames: {"Canada": {"948 Princess": ["Princess"]}}. Taken as definite matches.
PROPERTY_ALIASES = json.loads((ROOT / "property_aliases.json").read_text())

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
        "Amex 1001", "Amex 1007", "Mastercard 9906", "RBC Chequing 3290",
        "RBC Savings 1725", "BMO Chequing 9640", "RBC Savings", "Falis", "Cash",
    ],
}

# Other card numbers that bill to one of the accounts above (e.g. a second card on the same
# credit account). The receipt shows the card's own last 4, the statement shows the account's.
CARD_ALIASES = {
    "USA": {},
    "Canada": {
        "6827": "Mastercard 9906",  # RBC Business Mastercard card 6827 is on account ••9906
        "0430": "Amex 1001",        # Amex Canada card 0430 is on Amex Business Platinum ••1001
    },
}

# Bank Statements boards (filled by the bank sync from Plaid, one board per month per country).
# A board belongs to a country when it has that country's Plaid Transaction ID column; its month
# comes from the board name ("November", "November 2026", "Bank Statements · November 2026") with
# the year from the name or the board description.
BANK = {
    "USA": {
        "plaid_id": "text_mm8b35gj", "date": "date_mm8bn1ns", "account": "dropdown_mm8br48e",
        "money_out": "numeric_mm8bjz34", "money_in": "numeric_mm8bq5h1", "reconciliation": "color_mm8bvyje",
        "bank_status": "color_mm8bwq12", "notes": "long_text_mm8b7zfz",
    },
    "Canada": {
        "plaid_id": "text_mm8b5gtg", "date": "date_mm8byjx3", "account": "dropdown_mm8bb8ky",
        "money_out": "numeric_mm8bx64x", "money_in": "numeric_mm8bn387", "reconciliation": "color_mm8bt0rf",
        "bank_status": "color_mm8ben25", "notes": "long_text_mm8bdywe",
    },
}
# Last 4 digits of each synced bank account -> the expense board's Account label.
# Accounts not listed here (e.g. Amex 1007, Chase) aren't synced, so the bot can't check them.
BANK_ACCOUNTS = {
    "USA": {"2611": "Mercury", "8864": "Mercury", "3334": "Mercury", "0000": "Mercury"},
    "Canada": {
        "3290": "RBC Chequing 3290", "1725": "RBC Savings 1725", "9906": "Mastercard 9906",
        "9640": "BMO Chequing 9640", "1001": "Amex 1001",
    },
}
# A bank line is the same charge when it's dated this many days before/after the receipt.
BANK_DAYS_BEFORE, BANK_DAYS_AFTER = 2, 7
# Bills and invoices are often paid weeks later. If nothing fits the window above, a single line up
# to this many days after the receipt still counts (and decides which month the receipt is booked to).
BANK_DAYS_LATE = 45
# A refund credit shows up this many days after the refund receipt at most, and refunds a purchase
# made at most BANK_REFUND_ORIGINAL_DAYS before the credit.
BANK_REFUND_DAYS, BANK_REFUND_ORIGINAL_DAYS = 30, 90
# Reconciliation labels a person or the daily reconciliation has already settled.
BANK_SETTLED = ("Matched", "Not an Expense")

# "Category" dropdown labels. Utilities is worded differently per country.
_CATEGORIES = [
    "Cleaning", "Laundry & Linens", "Supplies & Consumables", "Repairs & Maintenance",
    "Handyman / Labour", "UTILITIES", "Internet & TV", "Garbage / Snow Removal",
    "Furnishings & Equipment", "Start-Up / Capex", "Software & Subscriptions",
    "Platform & Bank Fees", "Insurance", "Rent / Lease", "Marketing & Photography",
    "Travel & Vehicle", "Meals", "Office & Admin", "Professional Fees", "Donations",
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
