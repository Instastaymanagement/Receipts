"""Nightly run: Slack receipts channel -> Claude -> monday.com expense boards."""

import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import (
    BOARDS, CATEGORIES, COL, SUB_COL, COUNTRIES, DONE_REACTION, DRY_RUN, LOOKBACK_DAYS,
    MONDAY_ACCOUNT_URL, TIMEZONE, WARN_REACTION, env,
)
from .boards import BoardManager
from .extractor import IMAGE_TYPES, MAX_IMAGE_BYTES, Extractor, Receipt
from .matching import already_logged, find_statement_match, resolve_account
from .monday_api import Monday
from .schedule import should_run
from .slack_api import Slack

TZ = ZoneInfo(TIMEZONE)


@dataclass
class Stats:
    logged: dict = field(default_factory=lambda: {c: 0 for c in COUNTRIES})
    attached: int = 0
    needs_attention: int = 0
    errors: int = 0


@dataclass
class SlackFile:
    name: str
    mimetype: str
    original: bytes          # what gets uploaded to monday
    for_claude: bytes | None  # what Claude reads (a JPEG preview for HEIC etc.)
    claude_mimetype: str | None


class Problem(Exception):
    """Something the poster needs to fix. Shown in the Slack thread."""


def item_url(board_id: int, item_id: str) -> str:
    return f"{MONDAY_ACCOUNT_URL}/boards/{board_id}/pulses/{item_id}"


def load_files(slack: Slack, msg: dict) -> list[SlackFile]:
    files = []
    for f in msg.get("files", []):
        if f.get("mode") == "tombstone" or not f.get("url_private_download"):
            continue
        mimetype = f.get("mimetype", "")
        original = slack.download(f["url_private_download"])
        if mimetype == "application/pdf" or (mimetype in IMAGE_TYPES and len(original) <= MAX_IMAGE_BYTES):
            files.append(SlackFile(f.get("name", "receipt"), mimetype, original, original, mimetype))
            continue
        # HEIC photos or very large images: Claude reads Slack's preview image instead.
        thumb = next((f[k] for k in ("thumb_1024", "thumb_960", "thumb_720", "thumb_480") if f.get(k)), None)
        if thumb:
            thumb_type = "image/png" if thumb.lower().endswith(".png") else "image/jpeg"
            files.append(SlackFile(f.get("name", "receipt"), mimetype, original, slack.download(thumb), thumb_type))
        else:
            files.append(SlackFile(f.get("name", "receipt"), mimetype, original, None, None))
    return files


def thread_text(slack: Slack, channel: str, msg: dict) -> tuple[str, bool]:
    """Post text plus human thread replies. Second value: whether there's a reply newer than the bot's last one."""
    text = msg.get("text", "")
    if not msg.get("reply_count"):
        return text, False
    replies = slack.replies(channel, msg["ts"])
    bot_ts = [float(r["ts"]) for r in replies if r.get("user") == slack.bot_user_id or r.get("bot_id")]
    last_bot = max(bot_ts, default=0.0)
    human = [r for r in replies if r.get("user") != slack.bot_user_id and not r.get("bot_id")]
    if human:
        text += "\n\nThread replies (newest last):\n" + "\n".join(f"- {r.get('text', '')}" for r in human)
    return text, any(float(r["ts"]) > last_bot for r in human)


class Runner:
    def __init__(self):
        self.channel = env("SLACK_CHANNEL_ID")
        self.slack = Slack(env("SLACK_BOT_TOKEN"), dry_run=DRY_RUN)
        self.monday = Monday(env("MONDAY_API_TOKEN"), dry_run=DRY_RUN)
        self.properties = []
        for country in COUNTRIES:
            self.properties += self.monday.properties(country, BOARDS[country]["properties_board"])
        self.props_by_id = {p["id"]: p for p in self.properties}
        self.extractor = Extractor(env("ANTHROPIC_API_KEY"), self.properties)
        self.boards = BoardManager(self.monday, dry_run=DRY_RUN)
        self.board_items: dict[int, list[dict]] = {}
        self.stats = Stats()

    def items(self, board_id: int) -> list[dict]:
        if board_id not in self.board_items:
            self.board_items[board_id] = self.monday.expense_items(board_id)
        return self.board_items[board_id]

    def run(self) -> None:
        try:
            self.boards.ensure_upcoming(datetime.now(TZ).date())
        except Exception:
            self.stats.errors += 1
            print("Error creating monthly boards:", file=sys.stderr)
            traceback.print_exc()
        oldest = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
        for msg in self.slack.history(self.channel, oldest):
            if msg.get("bot_id") or msg.get("user") == self.slack.bot_user_id or not msg.get("files"):
                continue
            reacted = {r["name"]: r.get("users", []) for r in msg.get("reactions", [])}
            if self.slack.bot_user_id in reacted.get(DONE_REACTION, []):
                continue
            warned = self.slack.bot_user_id in reacted.get(WARN_REACTION, [])
            try:
                text, has_new_reply = thread_text(self.slack, self.channel, msg)
                if warned and not has_new_reply:
                    continue  # waiting for someone to reply with the fix
                self.process(msg, text, warned)
            except Exception:
                # Bot/API failure, not the poster's fault: no reaction, so it retries next run.
                self.stats.errors += 1
                print(f"Error on message {msg.get('ts')}:", file=sys.stderr)
                traceback.print_exc()
        self.post_summary()

    def process(self, msg: dict, text: str, warned: bool) -> None:
        ts = msg["ts"]
        posted = datetime.fromtimestamp(float(ts), TZ)
        poster = self.slack.user_name(msg.get("user", ""))
        files = load_files(self.slack, msg)
        readable = [(i, f) for i, f in enumerate(files) if f.for_claude]

        lines, problems = [], []
        if not readable:
            problems.append("I couldn't open the attachment. Please post a photo (JPG/PNG) or a PDF of the receipt.")
        else:
            extraction = self.extractor.extract(
                f"Posted by {poster} on {posted:%Y-%m-%d}.\n{text}",
                [(f.for_claude, f.claude_mimetype) for _, f in readable],
            )
            if not extraction.receipts:
                problems.append("I couldn't find a receipt in the attachment.")
            permalink = self.slack.permalink(self.channel, ts)
            for receipt in extraction.receipts:
                # Map Claude's indexes (into the readable list) back to the post's files.
                idx = [readable[i][0] for i in receipt.file_indexes if 0 <= i < len(readable)] or [i for i, _ in readable]
                try:
                    lines.append(self.log_receipt(receipt, [files[i] for i in idx], f"slack:{ts}:{'-'.join(map(str, idx))}",
                                                  posted.date(), poster, text, permalink))
                except Problem as p:
                    problems.append(f"*{receipt.vendor or 'Receipt'}*: {p}")

        if problems:
            self.stats.needs_attention += 1
            reply = "\n".join(lines + [f":warning: {p}" for p in problems])
            reply += "\nReply in this thread with the fix and I'll pick it up on the next nightly run."
            self.slack.reply(self.channel, ts, reply)
            self.slack.react(self.channel, ts, WARN_REACTION)
        else:
            self.slack.reply(self.channel, ts, "\n".join(lines))
            self.slack.react(self.channel, ts, DONE_REACTION)
            if warned:
                self.slack.unreact(self.channel, ts, WARN_REACTION)

    def log_receipt(self, r: Receipt, files: list[SlackFile], ref: str, posted: date,
                    poster: str, text: str, permalink: str) -> str:
        if not r.readable or r.total is None:
            raise Problem(r.problem or "I couldn't read the total on this receipt. Please post a clearer photo.")

        prop = self.props_by_id.get(r.property_id or "")
        if prop:
            country = prop["country"]
        elif r.is_overhead and r.country:
            country = r.country
        else:
            raise Problem(r.problem or "I couldn't match the property. Reply with the property name as it appears in Monday.")

        when, date_note = posted, " (no date on receipt, used post date)"
        if r.receipt_date:
            try:
                when, date_note = date.fromisoformat(r.receipt_date), ""
            except ValueError:
                pass

        month = when.strftime("%Y-%m")
        board_id = self.boards.board_for(country, month)
        if not board_id:
            raise Problem(f"The date reads {when:%b %d, %Y}, and there's no {country} expense board for that month "
                          f"(boards are only kept from {BOARDS['first_month']} on). If the date is wrong, reply with the correct one.")

        items = self.items(board_id)
        done = already_logged(items, ref)
        if done:
            return f":white_check_mark: Already logged: {item_url(board_id, done['id'])}"

        account = resolve_account(country, r.account, r.card_last4)
        category = r.category if r.category in CATEGORIES[country] else None
        expected_currency = "USD" if country == "USA" else "CAD"
        warnings = []
        if r.currency and r.currency != expected_currency:
            warnings.append(f"currency on receipt is {r.currency}, board is {expected_currency}")
        if not account:
            warnings.append("account not identified")
        if r.problem:
            warnings.append(r.problem)

        note = (
            f"From Slack ({ref}) by {poster} on {posted:%Y-%m-%d}: {text.strip()}\n{permalink}\n"
            f"Read by Claude: {r.description}. Confidence: {r.confidence}."
            + (f"\nCheck: {'; '.join(warnings)}." if warnings else "")
        )
        prop_label = prop["name"] if prop else "Company / Overhead"
        summary = (
            f"{r.vendor} · ${r.total:,.2f}" + (f" (tax ${r.tax:,.2f})" if r.tax else "")
            + f" · {when:%b %d}{date_note} · {prop_label} · {category or 'no category'} · {account or 'account unknown'}"
        )

        match = find_statement_match(items, r.total, when, account)
        if match:
            cols = match["cols"]
            update = {
                COL["receipt_status"]: {"label": "Uploaded"},
                COL["notes"]: {"text": (cols.get(COL["notes"], {}).get("text", "") + "\n\n" + note).strip()},
            }
            existing_prop = cols.get(COL["property"], {})
            if prop and not (existing_prop.get("text") or existing_prop.get("value")):
                update[COL["property"]] = {"item_ids": [int(prop["id"])]}
            if category and not cols.get(COL["category"], {}).get("text"):
                update[COL["category"]] = {"labels": [category]}
            self.monday.update_item(board_id, match["id"], update)
            item_id = match["id"]
            cols.setdefault(COL["notes"], {})["text"] = update[COL["notes"]]["text"]
            cols[COL["receipt_status"]] = {"text": "Uploaded", "value": None}
            self.stats.attached += 1
            verb = "Attached to existing line"
        else:
            values = {
                COL["date"]: {"date": when.isoformat()},
                COL["amount"]: f"{r.total:.2f}",
                COL["billable"]: {"label": "Unsure"},
                COL["report_month"]: {"labels": [when.strftime("%B %Y")]},
                COL["receipt_status"]: {"label": "Uploaded"},
                COL["review_status"]: {"label": "New"},
                COL["statement_ref"]: ref,
                COL["notes"]: {"text": note},
            }
            if r.tax is not None:
                values[COL["tax"]] = f"{r.tax:.2f}"
            if account:
                values[COL["account"]] = {"label": account}
            if prop:
                values[COL["property"]] = {"item_ids": [int(prop["id"])]}
            if category:
                values[COL["category"]] = {"labels": [category]}
            item_id = self.monday.create_item(board_id, r.vendor or "Receipt", values)
            items.append({"id": item_id, "name": r.vendor, "cols": {
                COL["statement_ref"]: {"text": ref}, COL["notes"]: {"text": note},
                COL["receipt_status"]: {"text": "Uploaded"},
            }})
            self.stats.logged[country] += 1
            verb = f"Logged to {country}"

        for f in files:
            self.monday.upload_file(item_id, f.name, f.original, f.mimetype)
        for li in r.line_items:
            sub = {}
            if li.quantity is not None:
                sub[SUB_COL["qty"]] = f"{li.quantity:g}"
            if li.price is not None:
                sub[SUB_COL["price"]] = f"{li.price:.2f}"
            self.monday.create_subitem(item_id, li.name[:250], sub)

        line = f":white_check_mark: {verb}: {summary}\n{item_url(board_id, item_id)}"
        if r.line_items:
            line += "\n" + "\n".join(
                f"   • {li.name}" + (f" ×{li.quantity:g}" if li.quantity and li.quantity != 1 else "")
                + (f" ${li.price:,.2f}" if li.price is not None else "")
                for li in r.line_items
            )
        if warnings:
            line += f"\n_Please check in Monday: {'; '.join(warnings)}._"
        return line

    def post_summary(self) -> None:
        s = self.stats
        total = sum(s.logged.values()) + s.attached
        if self.boards.created:
            self.slack.post(self.channel, "Created new expense board(s) on Monday: " + ", ".join(self.boards.created))
        if not (total or s.needs_attention or s.errors):
            print("No new receipts.")
            return
        parts = [f"*Receipts run {datetime.now(TZ):%b %d}:* {total} logged"]
        parts.append(f"({', '.join(f'{c} {n}' for c, n in s.logged.items())}, {s.attached} matched to existing statement lines)")
        if s.needs_attention:
            parts.append(f"· :warning: {s.needs_attention} post(s) need attention")
        if s.errors:
            parts.append(f"· {s.errors} failed on the bot side and will retry tomorrow")
        message = " ".join(parts)
        print(message)
        self.slack.post(self.channel, message)


def main() -> None:
    if not should_run(os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("SCHEDULE", "")):
        print("Not 9 pm in Toronto for this trigger; skipping.")
        return
    runner = Runner()
    runner.run()
    if runner.stats.errors:
        sys.exit(1)  # mark the GitHub run red so failures get noticed


if __name__ == "__main__":
    main()
