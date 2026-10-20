"""Minimal monday.com GraphQL client."""

import json

import requests

from .config import COL, PROPERTY_ALIAS_COLUMNS

API = "https://api.monday.com/v2"
FILE_API = "https://api.monday.com/v2/file"

ITEM_FIELDS = "id name column_values(ids: $cols) { id text value }"


class MondayError(RuntimeError):
    pass


class Monday:
    def __init__(self, token: str, dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        resp = requests.post(
            API,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": self.token},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors") or data.get("error_message"):
            raise MondayError(json.dumps(data.get("errors") or data.get("error_message")))
        return data["data"]

    def board_items(self, board_id: int, column_ids: list[str]) -> list[dict]:
        """All items on a board, with the requested columns flattened to {col_id: {text, value}}."""
        first = self._gql(
            f"""query ($board: [ID!], $cols: [String!]) {{
                  boards(ids: $board) {{ items_page(limit: 500) {{ cursor items {{ {ITEM_FIELDS} group {{ title }} }} }} }}
                }}""",
            {"board": [board_id], "cols": column_ids},
        )
        page = first["boards"][0]["items_page"]
        items = page["items"]
        cursor = page["cursor"]
        while cursor:
            nxt = self._gql(
                f"""query ($cursor: String!, $cols: [String!]) {{
                      next_items_page(limit: 500, cursor: $cursor) {{ cursor items {{ {ITEM_FIELDS} group {{ title }} }} }}
                    }}""",
                {"cursor": cursor, "cols": column_ids},
            )["next_items_page"]
            items += nxt["items"]
            cursor = nxt["cursor"]
        for item in items:
            item["cols"] = {cv["id"]: {"text": cv["text"] or "", "value": cv["value"]} for cv in item.pop("column_values")}
        return items

    def properties(self, country: str, board_id: int) -> list[dict]:
        alias_cols = PROPERTY_ALIAS_COLUMNS[country]
        out = []
        for item in self.board_items(board_id, alias_cols):
            aliases = [item["cols"][c]["text"] for c in alias_cols if item["cols"].get(c, {}).get("text")]
            out.append({
                "id": item["id"],
                "name": item["name"],
                "country": country,
                "area": (item.get("group") or {}).get("title", ""),
                "aliases": aliases,
            })
        return out

    def expense_items(self, board_id: int) -> list[dict]:
        return self.board_items(board_id, [
            COL["date"], COL["amount"], COL["account"], COL["receipt_status"],
            COL["statement_ref"], COL["notes"], COL["property"], COL["category"],
        ])

    # --- writes ---

    def create_item(self, board_id: int, name: str, column_values: dict) -> str:
        if self.dry_run:
            print(f"[dry-run] create on {board_id}: {name} {json.dumps(column_values)}")
            return "dry-run"
        data = self._gql(
            """mutation ($board: ID!, $name: String!, $cols: JSON!) {
                 create_item(board_id: $board, item_name: $name, column_values: $cols, create_labels_if_missing: true) { id }
               }""",
            {"board": board_id, "name": name, "cols": json.dumps(column_values)},
        )
        return data["create_item"]["id"]

    def update_item(self, board_id: int, item_id: str, column_values: dict) -> None:
        if self.dry_run:
            print(f"[dry-run] update {item_id} on {board_id}: {json.dumps(column_values)}")
            return
        self._gql(
            """mutation ($board: ID!, $item: ID!, $cols: JSON!) {
                 change_multiple_column_values(board_id: $board, item_id: $item, column_values: $cols, create_labels_if_missing: true) { id }
               }""",
            {"board": board_id, "item": item_id, "cols": json.dumps(column_values)},
        )

    def upload_file(self, item_id: str, filename: str, content: bytes, mimetype: str) -> None:
        if self.dry_run:
            print(f"[dry-run] upload {filename} ({len(content)} bytes) to item {item_id}")
            return
        query = f'mutation ($file: File!) {{ add_file_to_column(item_id: {int(item_id)}, column_id: "{COL["receipt"]}", file: $file) {{ id }} }}'
        resp = requests.post(
            FILE_API,
            headers={"Authorization": self.token},
            data={"query": query},
            files={"variables[file]": (filename, content, mimetype)},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors") or data.get("error_message"):
            raise MondayError(json.dumps(data.get("errors") or data.get("error_message")))
