from datetime import date

from receipt_bot.boards import BoardManager, board_name, next_month


class FakeMonday:
    def __init__(self, boards=None):
        self.boards = boards or []
        self.duplicated = []
        self.renamed = []

    def workspace_boards(self, ws):
        return list(self.boards)

    def duplicate_board(self, template, name, ws, folder):
        self.duplicated.append(name)
        return 999

    def board_groups(self, board_id):
        return [{"id": "g1", "title": "November 2026"}]

    def rename_group(self, board_id, group_id, title):
        self.renamed.append(title)


def test_names():
    assert board_name("2026-12") == "Expenses 2026-12 · December"
    assert next_month("2026-12") == "2027-01"
    assert next_month("2026-10") == "2026-11"


def test_existing_board_used():
    m = FakeMonday()
    assert BoardManager(m).board_for("Canada", "2026-10", date(2026, 10, 20)) == 18436307194
    assert m.duplicated == []


def test_found_by_name_in_folder():
    m = FakeMonday([{"id": "555", "name": "Expenses 2026-12 · December", "board_folder_id": "21667385"},
                    {"id": "556", "name": "Expenses 2026-12 · December", "board_folder_id": "1"}])
    assert BoardManager(m).board_for("USA", "2026-12", date(2026, 12, 3)) == 555
    assert m.duplicated == []


def test_creates_missing_month_and_renames_group():
    m = FakeMonday()
    mgr = BoardManager(m)
    assert mgr.board_for("USA", "2026-12", date(2026, 12, 3)) == 999
    assert m.duplicated == ["Expenses 2026-12 · December"]
    assert m.renamed == ["December 2026"]
    assert mgr.created == ["USA: Expenses 2026-12 · December"]
    assert mgr.board_for("USA", "2026-12", date(2026, 12, 3)) == 999  # cached, not created twice
    assert len(m.duplicated) == 1


def test_no_board_for_old_or_far_future_months():
    m = FakeMonday()
    mgr = BoardManager(m)
    assert mgr.board_for("USA", "2026-09", date(2026, 12, 3)) is None
    assert mgr.board_for("USA", "2027-03", date(2026, 12, 3)) is None
    assert m.duplicated == []


def test_ensure_upcoming_creates_next_month_from_25th():
    m = FakeMonday()
    BoardManager(m).ensure_upcoming(date(2026, 11, 20))
    assert m.duplicated == []  # November boards exist already
    BoardManager(m).ensure_upcoming(date(2026, 11, 26))
    assert m.duplicated == ["Expenses 2026-12 · December"] * 2  # USA and Canada
