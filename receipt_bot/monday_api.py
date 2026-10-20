"""Minimal monday.com GraphQL client."""

import json

import requests

from .config import COL, PROPERTY_ADDRESS_COLUMNS, PROPERTY_NAME_COLUMNS

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
        name_cols, addr_cols = PROPERTY_NAME_COLUMNS[country], PROPERTY_ADDRESS_COLUMNS[country]

        def texts(item, cols):
            seen = []
            for c in cols:
                t = item["cols"].get(c, {}).get("text", "").strip()
                if t and t not in seen:
                    seen.append(t)
            return seen

        return [
            {
                "id": item["id"],
                "name": item["name"],
                "country": country,
                "area": (item.get("group") or {}).get("title", ""),
                "aliases": texts(item, name_cols),
                "addresses": texts(item, addr_cols),
            }
            for item in self.board_items(board_id, name_cols + addr_cols)
        ]

    def expense_items(self, board_id: int) -> list[dict]:
        return self.board_items(board_id, [
            COL["date"], COL["amount"], COL["account"], COL["receipt_status"],
            COL["statement_ref"], COL["notes"], COL["property"], COL["category"],
        ])

    def workspace_boards(self, workspace_id: int) -> list[dict]:
        boards, page = [], 1
        while True:
            batch = self._gql(
                """query ($ws: [ID!], $page: Int!) {
                     boards(workspace_ids: $ws, state: active, limit: 500, page: $page) { id name board_folder_id }
                   }""",
                {"ws": [workspace_id], "page": page},
            )["boards"]
            boards += batch
            if len(batch) < 500:
                return boards
            page += 1

    def board_groups(self, board_id: int) -> list[dict]:
        return self._gql("query ($b: [ID!]) { boards(ids: $b) { groups { id title } } }", {"b": [board_id]})["boards"][0]["groups"]

    # --- writes ---

    def duplicate_board(self, template_id: int, name: str, workspace_id: int, folder_id: int) -> int:
        data = self._gql(
            """mutation ($b: ID!, $name: String!, $ws: ID!, $folder: ID!) {
                 duplicate_board(board_id: $b, duplicate_type: duplicate_board_with_structure, board_name: $name,
                                 workspace_id: $ws, folder_id: $folder, keep_subscribers: true) { board { id } }
               }""",
            {"b": template_id, "name": name, "ws": workspace_id, "folder": folder_id},
        )
        return int(data["duplicate_board"]["board"]["id"])

    def rename_group(self, board_id: int, group_id: str, title: str) -> None:
        self._gql(
            """mutation ($b: ID!, $g: String!, $t: String!) {
                 update_group(board_id: $b, group_id: $g, group_attribute: title, new_value: $t) { id }
               }""",
            {"b": board_id, "g": group_id, "t": title},
        )

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

    def create_subitem(self, parent_id: str, name: str, column_values: dict) -> None:
        if self.dry_run:
            print(f"[dry-run]   subitem under {parent_id}: {name} {json.dumps(column_values)}")
            return
        self._gql(
            """mutation ($parent: ID!, $name: String!, $cols: JSON!) {
                 create_subitem(parent_item_id: $parent, item_name: $name, column_values: $cols) { id }
               }""",
            {"parent": parent_id, "name": name, "cols": json.dumps(column_values)},
        )

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
