"""Read the Bank Statements boards so receipts can be checked against what the bank actually paid.

The bank sync (bankstatements repo) puts every transaction on a monthly Bank Statements board per
country. For each receipt the bot looks there for the line(s) that paid it: that tells it the
account when the receipt doesn't, and the bank line is marked Matched so the daily reconciliation
doesn't flag it.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .config import (
    BANK, BANK_ACCOUNTS, BANK_DAYS_AFTER, BANK_DAYS_BEFORE, BANK_DAYS_LATE, BANK_REFUND_DAYS,
    BANK_REFUND_ORIGINAL_DAYS, BANK_SETTLED, BOARDS,
)
from .matching import find_bank_match, find_refund_original
from .monday_api import Monday

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
_MONTH_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4}))?")


def board_month(name: str, description: str | None) -> str | None:
    """'YYYY-MM' for a Bank Statements board named e.g. "November", "November 2026" or
    "Bank Statements · November 2026" (year from the description when the name has none)."""
    m = _MONTH_RE.search(name or "")
    if not m:
        return None
    year = m.group(2) or (re.search(r"\b(20\d\d)\b", description or "") or [None, None])[1]
    if not year:
        return None
    return f"{year}-{MONTHS.index(m.group(1)) + 1:02d}"


def last4(text: str) -> str:
    m = re.search(r"(\d{4})\D*$", text or "")
    return m.group(1) if m else ""


def account_last4s(country: str, account: str | None) -> set[str] | None:
    """Bank accounts (last 4) an expense Account label can be paid from. None = unknown account, so
    any; an empty set = an account the bank sync doesn't cover (Cash, Falis, Amex 1007...)."""
    if not account:
        return None
    return {d for d, label in BANK_ACCOUNTS[country].items() if label == account}


def months_around(when: date, days_after: int = BANK_DAYS_AFTER) -> list[str]:
    """The receipt's month and every later month the bank could post it in within days_after."""
    months = [when.strftime("%Y-%m")]
    end = when + timedelta(days=days_after)
    y, m = when.year, when.month
    while (y, m) < (end.year, end.month):
        y, m = y + m // 12, m % 12 + 1
        months.append(f"{y}-{m:02d}")
    return months


def _amount(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (AttributeError, ValueError):
        return None


@dataclass
class BankMatch:
    rows: list[dict]
    account: str | None  # expense Account label for the bank account that paid
    original: dict | None = None  # for a refund: the matched purchase it gives money back on

    @property
    def paid(self) -> date:
        """When the bank paid it (the last line, for a split charge)."""
        return max(r["date"] for r in self.rows)

    def describe(self) -> str:
        r = self.rows[0]
        acct = r["account_text"] or r["last4"]
        if self.original:
            o = self.original
            return (f"{acct}: refund '{r['name']}' ${r['amount']:,.2f} on {r['date']:%b %d} (bank item {r['id']}), "
                    f"for the purchase of ${o['amount']:,.2f} on {o['date']:%b %d} (bank item {o['id']}, already matched)")
        if len(self.rows) == 1:
            return f"{acct}: '{r['name']}' ${r['amount']:,.2f} on {r['date']:%b %d} (bank item {r['id']})"
        parts = " + ".join(f"{x['amount']:.2f}" for x in self.rows)
        ids = ", ".join(x["id"] for x in self.rows)
        return f"{acct}: split charge {parts} = ${sum(x['amount'] for x in self.rows):,.2f}, '{r['name']}' (bank items {ids})"


class BankStatements:
    def __init__(self, monday: Monday):
        self.monday = monday
        self._boards: dict[str, dict[str, int]] | None = None
        self._rows: dict[int, list[dict]] = {}
        self._credits: dict[int, list[dict]] = {}  # Money In lines per board
        self.used: set[str] = set()  # bank lines claimed by a receipt earlier in this run

    def boards(self, country: str) -> dict[str, int]:
        if self._boards is None:
            self._boards = {c: {} for c in BANK}
            workspaces = {BOARDS[c]["workspace_id"] for c in BANK}
            for ws in workspaces:
                for b in self.monday.workspace_boards_with_columns(ws, [cfg["plaid_id"] for cfg in BANK.values()]):
                    if b["name"].startswith("Subitems of"):
                        continue
                    month = board_month(b["name"], b.get("description"))
                    for c, cfg in BANK.items():
                        if month and cfg["plaid_id"] in b["column_ids"]:
                            self._boards[c].setdefault(month, int(b["id"]))
        return self._boards[country]

    def rows(self, country: str, month: str) -> list[dict]:
        board_id = self.boards(country).get(month)
        if board_id is None:
            return []
        if board_id not in self._rows:
            self._load(country, board_id)
        return self._rows[board_id]

    def credits(self, country: str, month: str) -> list[dict]:
        """Money In lines (refunds, deposits) on a month's bank board."""
        board_id = self.boards(country).get(month)
        if board_id is None:
            return []
        if board_id not in self._credits:
            self._load(country, board_id)
        return self._credits[board_id]

    def _load(self, country: str, board_id: int) -> None:
        cfg = BANK[country]
        out, credits = [], []
        for item in self.monday.board_items(board_id, [cfg["date"], cfg["account"], cfg["money_out"], cfg["money_in"],
                                                       cfg["reconciliation"], cfg["bank_status"], cfg["notes"]]):
            cols = item["cols"]
            try:
                when = date.fromisoformat(cols.get(cfg["date"], {}).get("text", "")[:10])
            except ValueError:
                continue
            account_text = cols.get(cfg["account"], {}).get("text", "") or (item.get("group") or {}).get("title", "")
            row = {
                "id": item["id"], "board_id": board_id, "name": item["name"], "date": when,
                "last4": last4(account_text), "account_text": account_text,
                "reconciliation": cols.get(cfg["reconciliation"], {}).get("text", ""),
                "posted": cols.get(cfg["bank_status"], {}).get("text", "") != "Pending",
                "notes": cols.get(cfg["notes"], {}).get("text", ""),
            }
            money_out = _amount(cols.get(cfg["money_out"], {}).get("text", ""))
            money_in = _amount(cols.get(cfg["money_in"], {}).get("text", ""))
            if money_out and money_out > 0:
                out.append({**row, "amount": money_out})
            elif money_in and money_in > 0:
                credits.append({**row, "amount": money_in})
        self._rows[board_id], self._credits[board_id] = out, credits

    def find_refund(self, country: str, total: float, when: date, account: str | None) -> BankMatch | None:
        """The bank credit for a refund receipt, if its original purchase has already been matched.

        The credit is the only open Money In line for this amount dated from 2 days before to
        BANK_REFUND_DAYS after the refund receipt (on the receipt's account if known). The original is a
        Matched purchase from the same store on the same account, for at least this amount, within
        BANK_REFUND_ORIGINAL_DAYS before the credit. No matched original: None, so the credit stays for
        a person to review."""
        last4s = account_last4s(country, account)
        if last4s is not None and not last4s:
            return None
        months = months_around(when, BANK_REFUND_DAYS)
        credits = [r for m in months for r in self.credits(country, m)
                   if r["reconciliation"] not in BANK_SETTLED and r["id"] not in self.used]
        found = find_bank_match(credits, total, when, last4s, BANK_DAYS_BEFORE, BANK_REFUND_DAYS, strict=True)
        if not found:
            return None
        credit = found[0]
        earliest = credit["date"] - timedelta(days=BANK_REFUND_ORIGINAL_DAYS)
        purchases = [r for m in months_around(earliest, BANK_REFUND_ORIGINAL_DAYS + BANK_REFUND_DAYS)
                     for r in self.rows(country, m)]
        original = find_refund_original(purchases, credit, BANK_REFUND_ORIGINAL_DAYS)
        if not original:
            return None
        return BankMatch([credit], BANK_ACCOUNTS[country].get(credit["last4"]), original)

    def find(self, country: str, total: float, when: date, account: str | None,
             paid_month: str | None = None) -> BankMatch | None:
        """The bank line(s) that paid this receipt. paid_month: the receipt was booked to the month
        it was paid in, so look anywhere in that month's bank board."""
        last4s = account_last4s(country, account)
        if last4s is not None and not last4s:
            return None  # paid from an account the bank sync doesn't cover
        def open_rows(months):
            return [r for m in months for r in self.rows(country, m)
                    if r["reconciliation"] not in BANK_SETTLED and r["id"] not in self.used]

        if paid_month:
            found = find_bank_match(open_rows([paid_month]), total, when, last4s, BANK_DAYS_BEFORE, BANK_DAYS_AFTER,
                                    any_date=True)
        else:
            found = find_bank_match(open_rows(months_around(when)), total, when, last4s, BANK_DAYS_BEFORE,
                                    BANK_DAYS_AFTER)
            if not found:
                # Paid later (bills, invoices): one line for the full amount, up to BANK_DAYS_LATE after.
                found = find_bank_match(open_rows(months_around(when, BANK_DAYS_LATE)), total, when, last4s,
                                        BANK_DAYS_BEFORE, BANK_DAYS_LATE, strict=True)
        if not found:
            return None
        return BankMatch(found, BANK_ACCOUNTS[country].get(found[0]["last4"]))

    def claim(self, match: BankMatch, country: str, expense_board: int, item_id: str, vendor: str, today: date) -> None:
        """Mark the bank line(s) Matched to the expense. Lines still Pending only get reserved for this
        run: their amount can change, so the daily reconciliation matches them once they post."""
        cfg = BANK[country]
        split = ""
        if len(match.rows) > 1:
            parts = " + ".join(f"{r['amount']:.2f}" for r in match.rows)
            split = f" (split charge {parts} = {sum(r['amount'] for r in match.rows):.2f})"
        for r in match.rows:
            self.used.add(r["id"])
            if not r["posted"] or item_id == "dry-run":
                continue
            note = f"Expense: {vendor} (board {expense_board}, item {item_id}) — matched {today:%Y-%m-%d} by the receipt bot{split}"
            if match.original:
                note = (f"Refund: {vendor} (board {expense_board}, item {item_id}) — matched {today:%Y-%m-%d} by the receipt bot; "
                        f"refunds the purchase on bank item {match.original['id']} ({match.original['date']:%b %d}, "
                        f"${match.original['amount']:,.2f}), which is already matched")
            self.monday.update_item(r["board_id"], r["id"], {
                cfg["reconciliation"]: {"label": "Matched"},
                cfg["notes"]: {"text": (r["notes"] + "\n" + note).strip()},
            })
            r["reconciliation"] = "Matched"
