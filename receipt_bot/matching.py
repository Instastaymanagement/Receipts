"""Pure helpers: account resolution and matching receipts to existing board items."""

from datetime import date
from itertools import combinations

from .config import ACCOUNTS, CARD_ALIASES, COL, MATCH_DATE_WINDOW_DAYS


def resolve_account(country: str, claude_account: str | None, card_last4: str | None) -> str | None:
    labels = ACCOUNTS[country]
    if claude_account in labels:
        return claude_account
    if card_last4 and len(card_last4) == 4:
        alias = CARD_ALIASES.get(country, {}).get(card_last4)
        if alias in labels:
            return alias
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
        if cols.get(COL["statement_ref"], {}).get("text") == ref or f"({ref})" in cols.get(COL["notes"], {}).get("text", ""):
            return item
    return None


def split_logged(items: list[dict], base_ref: str, total: float) -> tuple[dict | None, str]:
    """For one file entered as several lines (base_ref, base_ref#1, ...): the line already logged
    with this amount, else None and the first free ref. Matching on amount keeps it right whatever
    order Claude returns the parts in."""
    used = set()
    for item in items:
        cols = item["cols"]
        refs = {cols.get(COL["statement_ref"], {}).get("text", "")}
        notes = cols.get(COL["notes"], {}).get("text", "")
        refs |= {w.strip("()") for w in notes.split() if w.strip("()").startswith(base_ref)}
        family = {x for x in refs if x == base_ref or x.startswith(base_ref + "#")}
        if not family:
            continue
        amount = _amount(cols.get(COL["amount"], {}).get("text", ""))
        if amount is not None and abs(amount - total) <= 0.01:
            return item, base_ref
        used |= family
    n = 0
    while (base_ref if n == 0 else f"{base_ref}#{n}") in used:
        n += 1
    return None, base_ref if n == 0 else f"{base_ref}#{n}"


def booking_month(receipt_date: date, paid_month: str | None) -> tuple[str, bool]:
    """Month whose board the receipt goes on, and whether a "paid in <month>" instruction moved it.

    Default is the receipt's own month. An explicit paid month from the Slack thread overrides it.
    """
    own = receipt_date.strftime("%Y-%m")
    if paid_month:
        try:
            month = date.fromisoformat(paid_month.strip()[:7] + "-01").strftime("%Y-%m")
        except ValueError:
            return own, False
        return month, month != own
    return own, False


def find_duplicate(items: list[dict], total: float, when: date, property_name: str | None) -> dict | None:
    """A receipt already logged from another Slack post for the same amount, date and property
    (e.g. the same bill posted twice)."""
    for item in items:
        cols = item["cols"]
        if cols.get(COL["receipt_status"], {}).get("text") != "Uploaded":
            continue
        if not cols.get(COL["statement_ref"], {}).get("text", "").startswith("slack:"):
            continue
        amount = _amount(cols.get(COL["amount"], {}).get("text", ""))
        if amount is None or abs(amount - total) > 0.01:
            continue
        if _parse_date(cols.get(COL["date"], {}).get("text", "")) != when:
            continue
        item_prop = cols.get(COL["property"], {}).get("text", "")
        if property_name and item_prop and item_prop != property_name:
            continue
        return item
    return None


def find_statement_match(items: list[dict], total: float, when: date, account: str | None,
                         any_date: bool = False) -> dict | None:
    """An existing line (e.g. from a card statement import) for the same charge that has no receipt yet.

    any_date: the receipt was booked to the month it was paid in, so the statement line can be
    dated anywhere on that month's board rather than near the receipt date.
    """
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
        if gap > MATCH_DATE_WINDOW_DAYS and not any_date:
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


def _merchant(name: str) -> str:
    """First word of a bank line's name, for grouping split charges ("Costco", "Amazon")."""
    words = "".join(c if c.isalnum() else " " for c in name.lower()).split()
    return words[0] if words else ""


def find_bank_match(rows: list[dict], total: float, when: date, last4s: set[str] | None,
                    days_before: int, days_after: int, any_date: bool = False,
                    strict: bool = False) -> list[dict] | None:
    """The bank line(s) that paid a receipt, or None if there isn't exactly one answer.

    rows: open Money Out lines from the Bank Statements boards, each {id, name, date, amount, last4, ...}.
    last4s: the accounts the receipt could have been paid from (None = any synced account).
    A line counts when it's dated days_before before to days_after after the receipt (anywhere in
    the rows when any_date, i.e. the receipt was booked to the month it was paid). One line for the
    whole amount wins; otherwise 2-4 lines from the same merchant on the same account that add up to
    it (Costco/Amazon charge each shipment separately).
    strict: only a single line, and only if it's the only one for that amount (no closest-date pick, no splits).
    """
    def fits(row):
        if last4s is not None and row["last4"] not in last4s:
            return False
        gap = (row["date"] - when).days
        return any_date or -days_before <= gap <= days_after

    pool = [r for r in rows if fits(r)]
    single = [r for r in pool if abs(r["amount"] - total) <= 0.01]
    if strict:
        return single if len(single) == 1 else None
    if single:
        gaps = sorted(abs((r["date"] - when).days) for r in single)
        if len(single) == 1 or gaps[0] < gaps[1]:
            return [min(single, key=lambda r: abs((r["date"] - when).days))]
        return None  # same amount on two lines equally close: let a person decide

    groups: dict[tuple, list[dict]] = {}
    for r in pool:
        if r["amount"] < total:
            groups.setdefault((r["last4"], _merchant(r["name"])), []).append(r)
    found = []
    for lines in groups.values():
        lines = sorted(lines, key=lambda r: r["date"])[:12]  # keeps the search small
        for size in (2, 3, 4):
            for combo in combinations(lines, size):
                if abs(sum(r["amount"] for r in combo) - total) <= 0.01:
                    found.append(list(combo))
                    if len(found) > 1:
                        return None
    return found[0] if found else None


def find_refund_original(purchases: list[dict], credit: dict, max_days: int) -> dict | None:
    """The purchase a refund credit gives money back on: a Matched Money Out line from the same store on
    the same account, for at least the refund amount, dated up to max_days before the credit. Prefers
    the same amount, then the latest purchase."""
    cands = [p for p in purchases
             if p["reconciliation"] == "Matched" and p["last4"] == credit["last4"]
             and _merchant(p["name"]) == _merchant(credit["name"])
             and p["amount"] >= credit["amount"] - 0.01
             and 0 <= (credit["date"] - p["date"]).days <= max_days]
    if not cands:
        return None
    exact = [p for p in cands if abs(p["amount"] - credit["amount"]) <= 0.01]
    return max(exact or cands, key=lambda p: p["date"])
