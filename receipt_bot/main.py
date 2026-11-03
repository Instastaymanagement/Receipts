"""Nightly run: Slack receipts channel -> Claude -> monday.com expense boards."""

import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import (
    BOARDS, CATEGORIES, COL, PROPERTY_ALIASES, SUB_COL, COUNTRIES, DONE_REACTION, DRY_RUN, LOOKBACK_DAYS,
    MONDAY_ACCOUNT_URL, TIMEZONE, WARN_REACTION, env,
)
from .bank import BankStatements
from .boards import BoardManager
from .extractor import IMAGE_TYPES, MAX_IMAGE_BYTES, Extractor, Receipt
from .matching import already_logged, booking_month, split_logged, find_duplicate, find_statement_match, resolve_account, resolve_property
from .monday_api import Monday
from .schedule import should_run
from .slack_api import Slack

TZ = ZoneInfo(TIMEZONE)
# Manual runs can re-check every ⚠️ post (e.g. after the matching rules change).
RETRY_WARNINGS = os.environ.get("RETRY_WARNINGS", "").lower() in ("1", "true", "yes")
# Manual runs can re-check specific ✅ posts (Slack message timestamps, comma separated).
# Receipts already on Monday are skipped, so only missing ones get added.
RECHECK = {t.strip() for t in os.environ.get("RECHECK_POSTS", "").split(",") if t.strip()}


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


def thread_text(slack: Slack, channel: str, msg: dict) -> tuple[str, bool, str]:
    """Post text plus human thread replies, whether there's a reply newer than the bot's last one,
    and the bot's last reply text."""
    text = msg.get("text", "")
    if not msg.get("reply_count"):
        return text, False, ""
    replies = slack.replies(channel, msg["ts"])
    bot = [r for r in replies if r.get("user") == slack.bot_user_id or r.get("bot_id")]
    last_bot = max(bot, key=lambda r: float(r["ts"]), default=None)
    last_bot_ts = float(last_bot["ts"]) if last_bot else 0.0
    human = [r for r in replies if r.get("user") != slack.bot_user_id and not r.get("bot_id")]
    if human:
        text += "\n\nThread replies (newest last):\n" + "\n".join(f"- {r.get('text', '')}" for r in human)
    return text, any(float(r["ts"]) > last_bot_ts for r in human), (last_bot or {}).get("text", "")


class Runner:
    def __init__(self):
        self.channel = env("SLACK_CHANNEL_ID")
        self.slack = Slack(env("SLACK_BOT_TOKEN"), dry_run=DRY_RUN)
        self.monday = Monday(env("MONDAY_API_TOKEN"), dry_run=DRY_RUN)
        self.properties = []
        for country in COUNTRIES:
            self.properties += self.monday.properties(country, BOARDS[country]["properties_board"])
        for p in self.properties:
            p["aliases"] += PROPERTY_ALIASES.get(p["country"], {}).get(p["name"], [])
        self.props_by_id = {p["id"]: p for p in self.properties}
        self.extractor = Extractor(env("ANTHROPIC_API_KEY"), self.properties)
        self.boards = BoardManager(self.monday, dry_run=DRY_RUN)
        self.bank = BankStatements(self.monday)
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
            if self.slack.bot_user_id in reacted.get(DONE_REACTION, []) and msg["ts"] not in RECHECK:
                continue
            warned = self.slack.bot_user_id in reacted.get(WARN_REACTION, [])
            try:
                text, has_new_reply, last_bot_text = thread_text(self.slack, self.channel, msg)
                if warned and not has_new_reply and not RETRY_WARNINGS:
                    continue  # waiting for someone to reply with the fix
                self.process(msg, text, warned, has_new_reply, last_bot_text)
            except Exception:
                # Bot/API failure, not the poster's fault: no reaction, so it retries next run.
                self.stats.errors += 1
                print(f"Error on message {msg.get('ts')}:", file=sys.stderr)
                traceback.print_exc()
        self.post_summary()

    def process(self, msg: dict, text: str, warned: bool, has_new_reply: bool, last_bot_text: str = "") -> None:
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
                [(f.for_claude, f.claude_mimetype, f.name) for _, f in readable],
            )
            if not extraction.receipts:
                problems.append("I couldn't find a receipt in the attachment.")
            permalink = self.slack.permalink(self.channel, ts)
            # Map Claude's indexes (into the readable list) back to the post's files.
            indexes = [[readable[i][0] for i in r.file_indexes if 0 <= i < len(readable)] or [i for i, _ in readable]
                       for r in extraction.receipts]
            refs = [f"slack:{ts}:{'-'.join(map(str, idx))}" for idx in indexes]
            for receipt, idx, ref in zip(extraction.receipts, indexes, refs):
                try:
                    # split: one file entered as several lines (e.g. a bill shared by two properties).
                    lines.append(self.log_receipt(receipt, [files[i] for i in idx], ref,
                                                  posted.date(), poster, text, permalink, split=refs.count(ref) > 1))
                except Problem as p:
                    problems.append(f"*{receipt.vendor or 'Receipt'}*: {p}")

        lines = [line for line in lines if line]  # None = logged on an earlier run
        if problems:
            self.stats.needs_attention += 1
            if warned and not has_new_reply and not lines and all(p in last_bot_text for p in problems):
                print(f"Still needs attention, already told the thread: {ts}")
                return  # retry run with nothing new: don't repeat the same warning
            reply = "\n".join(lines + [f":warning: {p}" for p in problems])
            reply += "\nReply in this thread with the fix and I'll pick it up on the next nightly run."
            self.slack.reply(self.channel, ts, reply)
            self.slack.react(self.channel, ts, WARN_REACTION)
        else:
            if not lines and ts in RECHECK and not warned:
                print(f"Re-checked, nothing missing: {ts}")
                return
            self.slack.reply(self.channel, ts, "\n".join(lines) or ":white_check_mark: All receipts in this post are now logged.")
            self.slack.react(self.channel, ts, DONE_REACTION)
            if warned:
                self.slack.unreact(self.channel, ts, WARN_REACTION)

    def log_receipt(self, r: Receipt, files: list[SlackFile], ref: str, posted: date,
                    poster: str, text: str, permalink: str, split: bool = False) -> str | None:
        if not r.readable or r.total is None:
            raise Problem(r.problem or "I couldn't read the total on this receipt. Please post a clearer photo.")

        prop_id, matched_by, prop_problem, prop_note = resolve_property(
            self.props_by_id, r.message_property_id, r.label_property_id, r.address_property_id,
            r.label_text, r.receipt_address)
        prop = self.props_by_id.get(prop_id or "")
        # Country when there's no property: Claude's reading, else the receipt's currency.
        country_hint = r.country or {"CAD": "Canada", "USD": "USA"}.get(r.currency or "")
        if r.category == "Donations":
            # Donations are company charity: no property, whatever the post or receipt says.
            if not country_hint:
                raise Problem("Is this donation from Canada or the USA? Reply with the country and I'll log it as charity.")
            prop, country, matched_by = None, country_hint, "donation (charity, no property)"
        elif r.is_fuel:
            # Gas-station fuel is an InstaStay company expense: no property.
            if not country_hint:
                raise Problem("Is this fuel purchase from Canada or the USA? Reply with the country and I'll log it as a company expense.")
            prop, country, matched_by = None, country_hint, "fuel (InstaStay company expense, no property)"
        elif prop:
            country = prop["country"]
        elif r.is_overhead and country_hint:
            country, matched_by = country_hint, "Slack post"
        else:
            raise Problem(prop_problem)
        # Refunds are logged as negative amounts, so they reduce the month's expenses.
        amount = -r.total if r.is_refund else r.total

        when, date_note = posted, " (no date on receipt, used post date)"
        if r.receipt_date:
            try:
                when, date_note = date.fromisoformat(r.receipt_date), ""
            except ValueError:
                pass

        # Default: the receipt's own month. "Paid in <month>" in the thread moves it to that month's board.
        month, moved = booking_month(when, r.paid_month)

        account = resolve_account(country, r.account, r.card_last4)
        category = r.category if r.category in CATEGORIES[country] else None
        if r.is_fuel:
            category = "Travel & Vehicle"
        expected_currency = "USD" if country == "USA" else "CAD"
        # What the bank says: the line(s) that paid this receipt, so the account if the receipt doesn't show it,
        # and the month it was actually paid in (bills are often paid the month after).
        bank = None
        if r.currency in (None, expected_currency) and not r.is_refund:
            try:
                bank = self.bank.find(country, r.total, when, account, month if moved else None)
            except Exception:
                print(f"Couldn't check the bank statements for {ref}:", file=sys.stderr)
                traceback.print_exc()
        if bank and not account and bank.account:
            account = bank.account
        bank_moved = False
        if bank and not moved and bank.paid.strftime("%Y-%m") != month:
            month, bank_moved = bank.paid.strftime("%Y-%m"), True

        month_label = f"{date.fromisoformat(month + '-01'):%B %Y}"
        board_id = self.boards.board_for(country, month)
        if not board_id:
            if moved:
                raise Problem(f"You said this was paid in {month_label}, but there's no {country} expense board for that month "
                              f"(boards are only kept from {BOARDS['first_month']} on). Reply with the right month.")
            raise Problem(f"The date reads {when:%b %d, %Y}, and there's no {country} expense board for that month "
                          f"(boards are only kept from {BOARDS['first_month']} on). If the date is wrong, reply with the "
                          f"correct one, or if it was paid in a later month, reply e.g. \"paid in October\".")
        if moved:
            date_note += f" (paid in {month_label} per Slack thread)"
        elif bank_moved:
            date_note += f" (paid {bank.paid:%b %d} per bank)"

        items = self.items(board_id)
        # A receipt moved to its bank month may already be on its own month's board from an earlier run.
        own_board = self.boards.board_for(country, when.strftime("%Y-%m")) if bank_moved else None
        if split:
            done, ref = split_logged(items, ref, amount)
        else:
            done = already_logged(items, ref) or (own_board and already_logged(self.items(own_board), ref))
        if done:
            return None

        warnings = []
        if r.is_refund:
            warnings.append("refund: logged as a negative amount and flagged Needs Review")
        if r.currency and r.currency != expected_currency:
            warnings.append(f"currency on receipt is {r.currency}, board is {expected_currency}")
        if not account:
            warnings.append("account not identified")
        if r.problem:
            warnings.append(r.problem)
        if prop_note and matched_by == "Slack post":
            warnings.append(prop_note)

        note = (
            f"From Slack ({ref}) by {poster} on {posted:%Y-%m-%d}: {text.strip() or '(no message)'}\n{permalink}\n"
            f"Read by Claude: {r.description}. Confidence: {r.confidence}."
            + f"\nProperty matched by: {matched_by}."
            + (f" File name/PO: {r.label_text}." if r.label_text else "")
            + (f" Shipped to: {r.receipt_address}." if r.receipt_address else "")
            + (f" ({prop_note}.)" if prop_note and matched_by != "Slack post" else "")
            + (f"\nBooked to {month_label}: receipt dated {when:%Y-%m-%d}, paid in {month_label} per Slack thread." if moved else "")
            + (f"\nBooked to {month_label}: receipt dated {when:%Y-%m-%d}, paid {bank.paid:%Y-%m-%d} per the bank statement." if bank_moved else "")
            + (f"\nBank: paid from {bank.describe()}." if bank else "")
            + (f"\nCheck: {'; '.join(w.rstrip('. ') for w in warnings)}." if warnings else "")
        )
        prop_label = prop["name"] if prop else (
            "Charity" if category == "Donations" else "InstaStay (company)" if r.is_fuel else "Company / Overhead")
        if prop and matched_by != "Slack post":
            prop_label += f" (matched by {matched_by})"
        summary = (
            f"{r.vendor} · {'refund ' if r.is_refund else ''}${amount:,.2f}" + (f" (tax ${r.tax:,.2f})" if r.tax else "")
            + f" · {when:%b %d}{date_note} · {prop_label} · {category or 'no category'} · {account or 'account unknown'}"
            + (f" (bank: {bank.rows[0]['date']:%b %d}" + (f", {len(bank.rows)} charges" if len(bank.rows) > 1 else "") + ")" if bank else "")
        )

        match = find_statement_match(items, amount, when, account, any_date=moved or bank_moved)
        if not match and not r.not_duplicate:
            dup = find_duplicate(items, amount, when, prop["name"] if prop else None)
            if dup:
                raise Problem(f"This looks like the same bill already logged from another post ({when:%b %d}, ${r.total:,.2f}): "
                              f"{item_url(board_id, dup['id'])}\nIf it's a separate charge, reply \"not a duplicate\" and I'll log it.")
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
            if account and not cols.get(COL["account"], {}).get("text"):
                update[COL["account"]] = {"label": account}
            if r.is_refund:
                update[COL["review_status"]] = {"label": "Needs Review"}
            self.monday.update_item(board_id, match["id"], update)
            item_id = match["id"]
            cols.setdefault(COL["notes"], {})["text"] = update[COL["notes"]]["text"]
            cols[COL["receipt_status"]] = {"text": "Uploaded", "value": None}
            self.stats.attached += 1
            verb = "Attached to existing line"
        else:
            values = {
                COL["date"]: {"date": when.isoformat()},
                COL["amount"]: f"{amount:.2f}",
                COL["billable"]: {"label": "Unsure"},
                COL["report_month"]: {"labels": [month_label]},
                COL["receipt_status"]: {"label": "Uploaded"},
                COL["review_status"]: {"label": "Needs Review" if r.is_refund else "New"},
                COL["statement_ref"]: ref,
                COL["notes"]: {"text": note},
            }
            if r.tax is not None:
                values[COL["tax"]] = f"{-r.tax if r.is_refund else r.tax:.2f}"
            if account:
                values[COL["account"]] = {"label": account}
            if prop:
                values[COL["property"]] = {"item_ids": [int(prop["id"])]}
            if category:
                values[COL["category"]] = {"labels": [category]}
            item_id = self.monday.create_item(board_id, r.vendor or "Receipt", values)
            items.append({"id": item_id, "name": r.vendor, "cols": {
                COL["statement_ref"]: {"text": ref}, COL["notes"]: {"text": note},
                COL["receipt_status"]: {"text": "Uploaded"}, COL["amount"]: {"text": f"{amount:.2f}"},
                COL["date"]: {"text": when.isoformat()}, COL["property"]: {"text": prop["name"] if prop else ""},
            }})
            self.stats.logged[country] += 1
            verb = f"Logged to {country}"

        if bank:
            try:
                self.bank.claim(bank, country, board_id, item_id, r.vendor or "Receipt", datetime.now(TZ).date())
            except Exception:
                print(f"Couldn't mark bank line(s) matched for {ref}:", file=sys.stderr)
                traceback.print_exc()
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
            line += f"\n_Please check in Monday: {'; '.join(w.rstrip('. ') for w in warnings)}._"
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
