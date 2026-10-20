"""Pure helpers: account resolution and matching receipts to existing board items."""

from datetime import date

from .config import ACCOUNTS, COL, MATCH_DATE_WINDOW_DAYS


def resolve_account(country: str, claude_account: str | None, card_last4: str | None) -> str | None:
    labels = ACCOUNTS[country]
    if claude_account in labels:
        return claude_account
    if card_last4 and len(card_last4) == 4:
        hits = [label for label in labels if label.endswith(card_last4)]
        if len(hits) == 1:
            return hits[0]
    return None


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _amount(text: str) -> float | None:
    try:
        return float(text.replace("$", "").replace(",", ""))
    except (AttributeError, ValueError):
        return None


def already_logged(items: list[dict], ref: str) -> dict | None:
    for item in items:
        cols = item["cols"]
        if cols.get(COL["statement_ref"], {}).get("text") == ref or ref in cols.get(COL["notes"], {}).get("text", ""):
            return item
    return None


def find_statement_match(items: list[dict], total: float, when: date, account: str | None) -> dict | None:
    """An existing line (e.g. from a card statement import) for the same charge that has no receipt yet."""
    best, best_gap = None, None
    for item in items:
        cols = item["cols"]
        if cols.get(COL["receipt_status"], {}).get("text") == "Uploaded":
            continue
        amount = _amount(cols.get(COL["amount"], {}).get("text", ""))
        if amount is None or abs(amount - total) > 0.01:
            continue
        item_date = _parse_date(cols.get(COL["date"], {}).get("text", ""))
        if item_date is None:
            continue
        gap = abs((item_date - when).days)
        if gap > MATCH_DATE_WINDOW_DAYS:
            continue
        item_account = cols.get(COL["account"], {}).get("text", "")
        if account and item_account and item_account != account:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = item, gap
    return best
