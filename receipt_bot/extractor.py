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
    receipt_address: str | None = Field(description="Ship-to, delivery, service or job-site address printed on the receipt/invoice (NOT the store's or vendor's own address), or null if none.")
    property_id: str | None = Field(description="ID of the matching property from the list, or null if no confident match.")
    property_source: Literal["message", "receipt_address", "both", "none"] = Field(description="What the property match is based on: the Slack message, the address on the receipt, both agreeing, or none.")
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
- Property: use two sources.
  1. The property named in the Slack message or thread. People use nicknames, street names, unit numbers or Hospitable names, so match generously.
  2. The address printed on the receipt that says where the goods/service went: Amazon/online "Ship to" / "Delivered to", utility "Service address", contractor "Job site", delivery address, etc. Compare it with each property's addresses (street number + street name is enough; ignore formatting, abbreviations like St/Street or Rd/Road, and missing postal codes). Units matter when the list has several units at one address (e.g. Upstairs/Downstairs, Unit A/B): only pick one if the receipt or message says which.
  Never use the store's or vendor's own address (the header address of Home Depot, Walmart, a restaurant, etc.) to match a property.
  If only one source gives a property, use it. If both give the same property, property_source = "both". If they point to different properties, return property_id null and explain the conflict in `problem`. Only return a property_id you are confident about; if two properties could match, return null and explain in `problem`.
- If the message says it's for the company, office, overhead or multiple properties, set is_overhead true, property_id null, and set country from context (USD/US address -> USA, CAD/Canadian address -> Canada) or null.
- Account: only choose from the account list for the property's country. Match on what the message says (e.g. "amex 1007", "BMO", "cash") or the card's last 4 digits. Otherwise null; don't guess.
- Category: pick the single best label from the category list for the property's country.
- Amounts: total is the final amount charged. Tax is sales tax / HST / GST included in that total.
- If a receipt is unreadable, isn't a receipt, or no property can be matched from the message or the receipt address, still return an entry with readable/property set accordingly and a clear one-line `problem` the poster can act on."""


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

    def extract(self, post_text: str, files: list[tuple[bytes, str]]) -> Extraction:
        content: list[dict] = []
        for i, (data, mimetype) in enumerate(files):
            content.append({"type": "text", "text": f"File {i}:"})
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
