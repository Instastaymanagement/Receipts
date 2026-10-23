"""Find (or create) the monthly expense board for a country.

New boards are copies of the country's template board (structure only), named
"Expenses YYYY-MM · Month" and placed in the expenses folder, so nobody has to
create boards or edit IDs by hand.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import BOARDS, COUNTRIES, TIMEZONE
from .monday_api import Monday

# Create next month's boards this many days before month end.
CREATE_AHEAD_FROM_DAY = 25


def toronto_today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def board_name(month: str) -> str:
    return f"Expenses {month} · {date.fromisoformat(month + '-01'):%B}"


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + m // 12}-{m % 12 + 1:02d}"


class BoardManager:
    def __init__(self, monday: Monday, dry_run: bool = False):
        self.monday = monday
        self.dry_run = dry_run
        self.created: list[str] = []
        self._found: dict[tuple[str, str], int] = {}
        self._workspace_boards: dict[str, list[dict]] = {}

    def _lookup(self, country: str, month: str) -> int | None:
        cfg = BOARDS[country]
        if month in cfg.get("existing_boards", {}):
            return cfg["existing_boards"][month]
        if country not in self._workspace_boards:
            self._workspace_boards[country] = self.monday.workspace_boards(cfg["workspace_id"])
        name = board_name(month)
        for b in self._workspace_boards[country]:
            if b["name"] == name and str(b.get("board_folder_id")) == str(cfg["expenses_folder_id"]):
                return int(b["id"])
        return None

    def board_for(self, country: str, month: str, today: date | None = None) -> int | None:
        """Board for a receipt month. Creates it if it's between first_month and next month."""
        key = (country, month)
        if key in self._found:
            return self._found[key]
        board_id = self._lookup(country, month)
        latest = next_month((today or toronto_today()).strftime("%Y-%m"))
        if board_id is None and BOARDS["first_month"] <= month <= latest:
            board_id = self._create(country, month)
        if board_id is not None:
            self._found[key] = board_id
        return board_id

    def ensure_upcoming(self, today: date | None = None) -> None:
        """This month's boards always; next month's from the 25th."""
        today = today or toronto_today()
        this = today.strftime("%Y-%m")
        months = [this] + ([next_month(this)] if today.day >= CREATE_AHEAD_FROM_DAY else [])
        for country in COUNTRIES:
            for month in months:
                self.board_for(country, month, today)

    def _create(self, country: str, month: str) -> int:
        cfg = BOARDS[country]
        name = board_name(month)
        group_title = f"{date.fromisoformat(month + '-01'):%B %Y}"
        if self.dry_run:
            print(f"[dry-run] would create {country} board '{name}' from template {cfg['template_board']}")
            return cfg["template_board"]  # read-only stand-in for the rest of the dry run
        board_id = self.monday.duplicate_board(cfg["template_board"], name, cfg["workspace_id"], cfg["expenses_folder_id"])
        for group in self.monday.board_groups(board_id):
            self.monday.rename_group(board_id, group["id"], group_title)
        self.monday.move_to_top(board_id, cfg["expenses_folder_id"])
        self._workspace_boards.setdefault(country, []).append(
            {"id": board_id, "name": name, "board_folder_id": cfg["expenses_folder_id"]})
        self.created.append(f"{country}: {name}")
        print(f"Created {country} board '{name}' ({board_id})")
        return board_id
