from datetime import date

from receipt_bot.bank import BankStatements, account_last4s, board_month, last4, months_around
from receipt_bot.config import BANK
from receipt_bot.matching import find_bank_match


def row(id, amount, when, last4="9906", name="Costco", **kw):
    return {"id": id, "board_id": 1, "name": name, "date": date.fromisoformat(when), "amount": amount,
            "last4": last4, "account_text": f"Account ••{last4}", "reconciliation": "Unmatched",
            "posted": True, "notes": "", **kw}


def match(rows, total, when, last4s=None, any_date=False):
    found = find_bank_match(rows, total, date.fromisoformat(when), last4s, 2, 7, any_date)
    return [r["id"] for r in found] if found else None


def test_single_line_within_window():
    rows = [row("a", 53.38, "2026-10-21"), row("b", 53.38, "2026-11-15")]
    assert match(rows, 53.38, "2026-10-19") == ["a"]
    assert match(rows, 53.38, "2026-10-01") is None  # bank date too far from the receipt


def test_account_filter():
    rows = [row("a", 50.0, "2026-10-05", last4="1001"), row("b", 50.0, "2026-10-05", last4="9640")]
    assert match(rows, 50.0, "2026-10-04", {"9640"}) == ["b"]
    assert match(rows, 50.0, "2026-10-04") is None  # two accounts, same day: ambiguous


def test_closest_of_two_wins_ties_dont():
    rows = [row("a", 53.09, "2026-10-09"), row("b", 53.09, "2026-10-14")]
    assert match(rows, 53.09, "2026-10-12") == ["b"]
    rows = [row("a", 20.0, "2026-10-10"), row("b", 20.0, "2026-10-14")]
    assert match(rows, 20.0, "2026-10-12") is None


def test_split_charges_same_merchant():
    rows = [row("a", 37.28, "2026-10-15"), row("b", 76.82, "2026-10-15"), row("c", 116.61, "2026-10-09"),
            row("d", 12.00, "2026-10-15", name="Smash")]
    assert sorted(match(rows, 114.10, "2026-10-13")) == ["a", "b"]
    rows = [row("a", 116.61, "2026-10-09"), row("b", 194.40, "2026-10-09"), row("c", 105.06, "2026-10-09"),
            row("d", 53.09, "2026-10-09")]
    assert sorted(match(rows, 469.16, "2026-10-07")) == ["a", "b", "c", "d"]


def test_split_never_mixes_merchants_or_accounts():
    rows = [row("a", 37.28, "2026-10-15"), row("b", 76.82, "2026-10-15", name="Walmart")]
    assert match(rows, 114.10, "2026-10-13") is None
    rows = [row("a", 37.28, "2026-10-15"), row("b", 76.82, "2026-10-15", last4="1001")]
    assert match(rows, 114.10, "2026-10-13") is None


def test_paid_month_looks_anywhere():
    rows = [row("a", 146.90, "2026-11-02", last4="9640", name="INTERAC ETRNSFR SENT PRIYANK")]
    assert match(rows, 146.90, "2026-10-20") is None
    assert match(rows, 146.90, "2026-10-20", any_date=True) == ["a"]


def test_helpers():
    assert board_month("November", "Bank transactions posted in November 2026, filed here.") == "2026-11"
    assert board_month("Bank Statements · October 2026", None) == "2026-10"
    assert board_month("October", None) is None
    assert last4("RBC Business Mastercard ••9906") == "9906"
    assert last4("Unrecognized account") == ""
    assert months_around(date(2026, 10, 10)) == ["2026-10"]
    assert months_around(date(2026, 12, 29)) == ["2026-12", "2027-01"]
    assert account_last4s("Canada", None) is None
    assert account_last4s("Canada", "BMO Chequing 9640") == {"9640"}
    assert account_last4s("Canada", "Cash") == set()
    assert account_last4s("USA", "Mercury") == {"2611", "8864", "3334", "0000"}


class FakeMonday:
    def __init__(self):
        self.updates = []

    def workspace_boards_with_columns(self, ws, cols):
        return [
            {"id": "11", "name": "November", "description": "Bank transactions posted in November 2026.",
             "column_ids": {BANK["Canada"]["plaid_id"]}},
            {"id": "12", "name": "November", "description": "Expenses", "column_ids": set()},
        ]

    def board_items(self, board_id, cols):
        c = BANK["Canada"]

        def item(id, name, when, out, account, recon="Unmatched", status="Posted"):
            return {"id": id, "name": name, "group": {"title": account}, "cols": {
                c["date"]: {"text": when}, c["account"]: {"text": account}, c["money_out"]: {"text": out},
                c["reconciliation"]: {"text": recon}, c["bank_status"]: {"text": status}, c["notes"]: {"text": ""}}}
        return [
            item("p1", "INTERAC ETRNSFR SENT PRIYANK", "2026-11-02", "146.9", "BMO Chequing ••9640"),
            item("p2", "Walmart", "2026-11-02", "93.32", "Amex Business Platinum ••1001", status="Pending"),
            item("p3", "Costco", "2026-11-03", "40", "RBC Business Mastercard ••9906", recon="Matched"),
            item("p4", "Deposit", "2026-11-03", "", "RBC Chequing ••3290"),
        ]

    def update_item(self, board_id, item_id, values):
        self.updates.append((board_id, item_id, values))


def test_bank_statements_find_and_claim():
    fake = FakeMonday()
    bank = BankStatements(fake)
    m = bank.find("Canada", 146.90, date(2026, 10, 29), None, paid_month="2026-11")
    assert m.account == "BMO Chequing 9640" and [r["id"] for r in m.rows] == ["p1"]
    bank.claim(m, "Canada", 500, "900", "Priyank Mistri", date(2026, 11, 3))
    board, item, values = fake.updates[0]
    assert (board, item) == (11, "p1")
    assert values[BANK["Canada"]["reconciliation"]] == {"label": "Matched"}
    assert "item 900" in values[BANK["Canada"]["notes"]]["text"]
    # Claimed lines aren't offered again in the same run.
    assert bank.find("Canada", 146.90, date(2026, 10, 29), None, paid_month="2026-11") is None

    # A pending line gives the account but isn't marked Matched yet.
    m = bank.find("Canada", 93.32, date(2026, 11, 2), None)
    assert m.account == "Amex 1001"
    bank.claim(m, "Canada", 500, "901", "Walmart", date(2026, 11, 3))
    assert len(fake.updates) == 1

    # Settled lines are ignored, and accounts the bank doesn't cover are skipped.
    assert bank.find("Canada", 40.0, date(2026, 11, 3), None) is None
    assert bank.find("Canada", 146.90, date(2026, 11, 2), "Cash") is None
