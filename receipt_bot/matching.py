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


def resolve_property(valid_ids, message_id, label_id, address_id, label_text=None, address_text=None):
    """Decide the property from the three sources.

    Returns (property_id or None, how it was matched, problem or None, warning or None).
    Order: the Slack message wins; otherwise file name/PO and shipping address must agree
    if both match; an unlisted shipping address falls back to the file name/PO.
    """
    message_id = message_id if message_id in valid_ids else None
    label_id = label_id if label_id in valid_ids else None
    address_id = address_id if address_id in valid_ids else None
    label_desc = f"file name/PO '{label_text}'" if label_text else "file name/PO"
    addr_desc = f"shipping address '{address_text}'" if address_text else "shipping address"

    if message_id:
        others = [d for i, d in ((label_id, label_desc), (address_id, addr_desc)) if i and i != message_id]
        warning = f"{' and '.join(others)} point to a different property than the post" if others else None
        return message_id, "Slack post", None, warning
    if label_id and address_id:
        if label_id == address_id:
            return label_id, "file name/PO + shipping address", None, None
        return None, "", f"The {label_desc} and the {addr_desc} point to different properties. Reply with the right one.", None
    if label_id:
        note = f"{addr_desc} isn't a listed property" if address_text else None
        return label_id, "file name/PO", None, note
    if address_id:
        return address_id, "shipping address", None, None
    found = [d for t, d in ((label_text, label_desc), (address_text, addr_desc)) if t]
    detail = (" (" + " and ".join(found) + " didn't match a listed property)") if found else ""
    return None, "", f"No property in the post, file name/PO or shipping address{detail}. Reply with the property name.", None
