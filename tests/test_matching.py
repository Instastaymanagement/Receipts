from datetime import date

from receipt_bot.matching import already_logged, find_statement_match, resolve_account


def item(id, amount, when, account="", receipt="Missing", ref="", notes=""):
    return {"id": id, "cols": {
        "amount": {"text": amount}, "exp_date": {"text": when}, "account": {"text": account},
        "receipt_status": {"text": receipt}, "statement_ref": {"text": ref}, "notes": {"text": notes},
    }}


def test_resolve_account():
    assert resolve_account("USA", "Amex 1007", None) == "Amex 1007"
    assert resolve_account("USA", "BMO Chequing 3290", None) is None  # Canada label on a US receipt
    assert resolve_account("Canada", None, "3290") == "BMO Chequing 3290"
    assert resolve_account("Canada", None, "0000") is None


def test_statement_match_picks_closest_unreceipted_line():
    items = [
        item("a", "84.12", "2026-11-01"),
        item("b", "84.12", "2026-11-04"),
        item("c", "84.12", "2026-11-03", receipt="Uploaded"),
        item("d", "84.10", "2026-11-03"),
    ]
    assert find_statement_match(items, 84.12, date(2026, 11, 3), None)["id"] == "b"


def test_statement_match_respects_account_and_window():
    items = [item("a", "50", "2026-11-03", account="Amex 1002"), item("b", "50", "2026-11-10")]
    assert find_statement_match(items, 50.0, date(2026, 11, 3), "Amex 1007") is None


def test_already_logged_by_ref_or_note():
    items = [item("a", "1", "2026-11-01", ref="slack:1.2:0"), item("b", "1", "2026-11-01", notes="From Slack (slack:3.4:0)")]
    assert already_logged(items, "slack:1.2:0")["id"] == "a"
    assert already_logged(items, "slack:3.4:0")["id"] == "b"
    assert already_logged(items, "slack:5.6:0") is None
