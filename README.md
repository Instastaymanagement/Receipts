# Receipts bot

Every night at **9 pm Toronto time**, this reads new receipts posted in the Slack receipts channel and enters them on the monthly expense board in **USA Automated** or **Canada Automated** on Monday.com.

```
Slack #expense-receipts ──► GitHub Actions (nightly) ──► Claude reads each receipt
                                                        ──► picks USA or Canada from the property
                                                        ──► monday.com expense board for that month
                                                        ──► ✅ / ⚠️ reply in the Slack thread
```

## How the team posts

Post the receipt photo or PDF in the channel with the property name in the same message:

> Property: 46 Cortland
> Paid: Amex 1007 *(optional)*
> Note: smoke detector replacement *(optional)*

- Several photos of the same receipt in one post are fine. So are several different receipts in one post.
- For costs that aren't tied to one property, write `Property: Overhead USA` or `Property: Overhead Canada`.
- After the nightly run, the bot replies in the thread:
  - **✅** The receipt was logged. The reply links to the Monday item.
  - **⚠️** Something needs fixing, such as an unknown property or an unreadable photo. **Reply in the thread** with the fix (e.g. "Property: 750 Walker"), and the next nightly run tries again.

## What it writes to Monday

| Column | Value |
|---|---|
| Name | Vendor |
| Date | Receipt date (or the post date if the receipt has none) |
| Amount / Sales Tax (USA) / HST Paid (Canada) | Read from the receipt |
| Account | From the post, or the card's last 4 digits. Left blank if unknown |
| Property | Linked from Properties Master (USA/Canada) |
| Category | Best match from the board's categories |
| Billable to Owner | `Unsure` |
| Owner Report Month | Month of the receipt |
| Receipt / Receipt Status | File uploaded / `Uploaded` |
| Review Status | `New` |
| Statement Ref | `slack:<message ts>:<file #>` (prevents double entry) |
| Notes | Poster, message, Slack link, Claude's description, and anything to double-check |

**Matching statement lines:** if the board already has a line with the same amount, a date within ±3 days, a matching (or blank) account, and no receipt yet, the bot attaches the receipt to that line instead of creating a duplicate.

## One-time setup

### 1. Slack app
1. Go to https://api.slack.com/apps → **Create New App** → **From scratch** → name it "Receipt Bot".
2. Under **OAuth & Permissions → Bot Token Scopes**, add `channels:history`, `groups:history`, `files:read`, `chat:write`, `reactions:read`, `reactions:write` and `users:read`.
3. Click **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-…`).
4. Create the channel (e.g. `#expense-receipts`), then run `/invite @Receipt Bot` in it.
5. Get the channel ID: open the channel's details, and the ID (`C0…`) is at the bottom.

### 2. API keys
- **monday.com:** Avatar → **Developers** → **My access tokens**. The token's owner needs access to both workspaces.
- **Anthropic:** https://console.anthropic.com → API keys.

### 3. GitHub settings for this repo (Settings → Secrets and variables → Actions)
| Type | Name | Value |
|---|---|---|
| Secret | `SLACK_BOT_TOKEN` | `xoxb-…` |
| Secret | `MONDAY_API_TOKEN` | monday token |
| Secret | `ANTHROPIC_API_KEY` | `sk-ant-…` |
| Variable | `SLACK_CHANNEL_ID` | `C0…` |
| Variable | `RECEIPTS_ENABLED` | `true` *(set this last, once testing passes)* |

### 4. Test
In **Actions → Nightly receipts → Run workflow**, keep **Dry run** ticked. The log shows what would be written, and nothing changes in Monday or Slack. Untick it to do a real run on demand.

## Monthly upkeep

Before the 1st of each month, create the new month's expense boards. Then add their IDs to `boards.json`:

```json
"expense_boards": { "2026-11": 18435986315, "2026-12": <new board id> }
```

The board ID is the number in the board's URL. If a month is missing, receipts for that month get ⚠️ until it's added.

## Running locally

```bash
pip install -r requirements.txt
export SLACK_BOT_TOKEN=… SLACK_CHANNEL_ID=… MONDAY_API_TOKEN=… ANTHROPIC_API_KEY=…
DRY_RUN=true python -m receipt_bot.main
pytest -q
```

## Files

- `receipt_bot/main.py`: the nightly run (Slack → Claude → Monday → Slack reply)
- `receipt_bot/extractor.py`: Claude prompt and output fields
- `receipt_bot/matching.py`: account lookup, duplicate and statement-line matching
- `receipt_bot/schedule.py`: 9 pm Toronto check (handles daylight saving)
- `receipt_bot/config.py`: column IDs, account and category labels
- `boards.json`: which Monday board each month goes to
- `.github/workflows/receipts.yml`: cron schedule
