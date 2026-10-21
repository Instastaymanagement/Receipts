"""Read receipts with Claude and return structured fields."""

import base64
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .config import ACCOUNTS, CATEGORIES, CLAUDE_MODEL

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class LineItem(BaseModel):
    name: str = Field(description="Item as a person would describe it, e.g. 'Washable hallway runner rug 2x8 ft'. Expand cryptic receipt abbreviations where you can.")
    quantity: float | None = Field(description="Quantity, or null if not shown.")
    price: float | None = Field(description="Line total for this item before tax (quantity x unit price, after any line discount), or null if not shown.")


class Receipt(BaseModel):
    file_indexes: list[int] = Field(description="Which attached files (0-based) make up this receipt. Several pages of one receipt = one entry.")
    readable: bool = Field(description="False if the file is not a receipt/invoice or the total cannot be read.")
    problem: str | None = Field(description="If something is missing or unclear, a short note for the team. Otherwise null.")
    vendor: str = Field(description="Store or company name, e.g. 'Home Depot'.")
    description: str = Field(description="Short summary of what was bought, e.g. 'Hallway rug and rug pad'.")
    line_items: list[LineItem] = Field(description="Every individual item on the receipt, in order. Do not skip or group items. Leave out tax, totals, payment and discount-only lines.")
    receipt_date: str | None = Field(description="Purchase date as YYYY-MM-DD, or null if not shown.")
    total: float | None = Field(description="Grand total actually charged, including tax and tip. Positive number.")
    tax: float | None = Field(description="Sales tax / HST included in the total. 0 if none shown, null if unclear.")
    currency: Literal["USD", "CAD", "OTHER"] | None
    card_last4: str | None = Field(description="Last 4 digits of the card used, if printed on the receipt.")
    country: Literal["USA", "Canada"] | None = Field(description="Country of the matched property.")
    message_property_id: str | None = Field(description="Property named in the Slack message/thread, matched to the list. Null if the post names none or it can't be matched confidently.")
    label_text: str | None = Field(description="Property name/hint found in the attachment's FILE NAME or in a PO / reference / job / order-note field on the receipt (e.g. 'Thistledown', 'Sarnia'). Null if none.")
    label_property_id: str | None = Field(description="Property that label_text refers to, matched to the list. Null if label_text is null or matches no listed property.")
    receipt_address: str | None = Field(description="Ship-to, delivery, service or job-site address printed on the receipt (NOT the store's or vendor's own address). Null if none.")
    address_property_id: str | None = Field(description="Property whose address matches receipt_address. Null if there's no address or it isn't a listed property address.")
    is_overhead: bool = Field(description="True if the post says it is a company / overhead / shared cost rather than one property.")
    account: str | None = Field(description="Exact account label from the country's list if the post or card digits identify it, else null.")
    category: str = Field(description="Exact category label from the list.")
    confidence: Literal["high", "medium", "low"]


class Extraction(BaseModel):
    receipts: list[Receipt]


SYSTEM = """You read expense receipts that a short-term-rental property management team posts in Slack, and turn them into bookkeeping entries.

Each Slack post has one or more attached files plus a message that names the property (and sometimes the card or account used, and notes). Thread replies may correct or add details; later replies override earlier text.

Rules:
- Return one entry per distinct receipt. Multiple photos/pages of the same receipt are one entry.
- Property: report three independent sources. Fill each from its own evidence only; don't copy one into another. Code combines them afterwards.
  1. message_property_id: the property named in the Slack message or thread. People use nicknames, street names, unit numbers or Hospitable names, so match generously.
  2. label_text / label_property_id: a property name in the attachment's file name (given before each file, e.g. "Thistledown receipt.pdf") or written in a PO number / reference / job name / order note on the receipt. Match it to the property list by name or street name.
  3. receipt_address / address_property_id: the address where the goods/service went (Amazon "Ship to", utility "Service address", contractor job site, delivery address). Compare with each property's addresses: street number + street name is enough; ignore formatting, St/Street, Rd/Road, missing postal codes. Never use the store's or vendor's own address. Many deliveries go to a team member's home, so an address that isn't in the list is normal: leave address_property_id null.
  Units matter when several properties share a street address or name (e.g. Upstairs/Downstairs, Unit 1/2): only pick one if the evidence says which unit; otherwise leave that source null and say so in `problem`.
- If the message says it's for the company, office, overhead or multiple properties, set is_overhead true, property_id null, and set country from context (USD/US address -> USA, CAD/Canadian address -> Canada) or null.
- `problem` is for things the team must fix or check. Don't use it to report that a shipping address isn't a listed property (that's normal); only mention property matching there when a unit is ambiguous or the sources contradict each other.
- Account: only choose from the account list for the property's country. Match on what the message says (e.g. "amex 1007", "BMO", "cash") or the card's last 4 digits. Otherwise null; don't guess.
- Category: pick the single best label from the category list for the property's country.
- Amounts: total is the final amount charged. Tax is sales tax / HST / GST included in that total.
- If a receipt is unreadable, isn't a receipt, still return an entry with readable/property set accordingly and a clear one-line `problem` the poster can act on."""


def _reference_text() -> str:
    return (
        "Account labels by country:\n" + json.dumps(ACCOUNTS, indent=1)
        + "\n\nCategory labels by country:\n" + json.dumps(CATEGORIES, indent=1)
    )


def file_block(content: bytes, mimetype: str) -> dict | None:
    data = base64.standard_b64encode(content).decode()
    if mimetype == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if mimetype in IMAGE_TYPES and len(content) <= MAX_IMAGE_BYTES:
        return {"type": "image", "source": {"type": "base64", "media_type": mimetype, "data": data}}
    return None


class Extractor:
    def __init__(self, api_key: str, properties: list[dict]):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.properties_text = "Property list (id | name | country | area | other names | addresses):\n" + "\n".join(
            f"{p['id']} | {p['name']} | {p['country']} | {p['area']} | {'; '.join(p['aliases'])} | {'; '.join(p['addresses'])}"
            for p in properties
        )

    def extract(self, post_text: str, files: list[tuple[bytes, str, str]]) -> Extraction:
        content: list[dict] = []
        for i, (data, mimetype, filename) in enumerate(files):
            content.append({"type": "text", "text": f'File {i}, file name "{filename}":'})
            content.append(file_block(data, mimetype))
        content.append({"type": "text", "text": f"Slack post (and thread replies):\n{post_text or '(no text)'}"})

        response = self.client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            output_config={"effort": "medium"},
            system=[
                {"type": "text", "text": SYSTEM},
                # Stable per run, so cache it across the night's receipts.
                {"type": "text", "text": _reference_text() + "\n\n" + self.properties_text, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": content}],
            output_format=Extraction,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"Claude did not return a result (stop_reason={response.stop_reason})")
        return response.parsed_output
