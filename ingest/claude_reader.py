"""Ask Claude to read a site plan (image, PDF, or text) into the extraction schema.

Uses claude-opus-5-5 with structured outputs, so the reply is valid JSON for
EXTRACTION_SCHEMA. Server-side refusal fallbacks are on. The API key comes from the
server's environment (ANTHROPIC_API_KEY or an `ant auth login` profile) and never
reaches the browser.
"""
import base64
import json

import anthropic

from .schema import EXTRACTION_SCHEMA

MODEL = "claude-opus-5-5"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

SYSTEM = """You read oil field site plans for a night-shift console that manages produced water (salt water) from wells.

Return every piece of equipment shown on the plan, using the schema.

Rules:
- Only include equipment that is drawn or written on the plan. Never add equipment a typical field would have if it is not shown.
- Copy each label exactly as written. If an item has no label, describe it briefly, like "unlabeled tank, top left".
- Give positions as fractions of the drawing: x is 0 at the left edge and 1 at the right edge, y is 0 at the top edge and 1 at the bottom edge. Use null if the plan has no layout, like a table.
- Fill a number only when it is written on the plan. Otherwise use null. Convert units to barrels and barrels per hour when the plan states them clearly.
- shutdown_pct is a tank's shutdown (shut-in) level in percent. lost_usd_hr is what a well loses per hour while shut in. Fill them only when written. If the plan states one shutdown level for every tank, put it on each tank and say so in evidence.
- manual is for crossover valves: true if the plan marks the valve manual (someone drives out to turn it), false if it marks it remote or automated, null if it doesn't say.
- feeds_label is the label of the tank a well or pump is connected to, if the plan shows it.
- connects_labels lists the labels a valve connects, in order.
- confidence is from 0 to 1: how sure you are that the item is real and the kind is right.
- evidence says, in a few words, what on the plan supports the item.
- meters_per_drawing_width is the real-world width the drawing covers, only if a scale bar or a dimension lets you work it out. Otherwise null.
- Put anything you can see but cannot interpret into unclear.

Kinds:
- tank: produced water storage tank
- pump: transfer pump moving water out of a tank
- discharge_valve: valve between a pump and the disposal line
- crossover_valve: valve connecting two tanks
- well: producing well or pumpjack
- disposal_well: saltwater disposal or injection well or facility
- truck: vacuum or water truck
- yard: truck yard or field office"""


class IngestError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.message, self.status = message, status


def _content(data, media_type, text):
    if media_type in IMAGE_TYPES:
        if len(data) > MAX_IMAGE_BYTES:
            raise IngestError("Images must be under 5 MB. Export the plan at a lower resolution.", 413)
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(data).decode()}}
        return [block, {"type": "text", "text": "Read this site plan."}]
    if media_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(data).decode()}}
        return [block, {"type": "text", "text": "Read this site plan."}]
    return [{"type": "text", "text": f"Read this site plan, given as text:\n\n{text}"}]


def read_plan(data, media_type, text=None, client=None):
    """Return the parsed extraction dict and request metadata. Raises IngestError."""
    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
            messages=[{"role": "user", "content": _content(data, media_type, text)}],
        )
    except anthropic.AuthenticationError:
        raise IngestError("The server's Anthropic API key is missing or invalid.", 503)
    except anthropic.RateLimitError:
        raise IngestError("Claude is rate limited right now. Try again in a minute.", 429)
    except anthropic.BadRequestError as exc:
        raise IngestError(f"Claude could not accept this file: {exc.message}", 400)
    except anthropic.APIStatusError as exc:
        raise IngestError(f"Claude returned an error ({exc.status_code}). Try again.", 502)
    except anthropic.APIConnectionError:
        raise IngestError("Could not reach Claude. Check the server's network.", 503)
    except anthropic.AnthropicError as exc:
        raise IngestError(f"Claude is not configured on the server: {exc}", 503)
    except TypeError as exc:
        # The SDK raises a plain TypeError when it finds no credentials at all.
        if "authentication" in str(exc).lower():
            raise IngestError("Reading images and PDFs needs ANTHROPIC_API_KEY on the server.", 503)
        raise

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise IngestError(f"Claude declined to read this file (category: {category}).", 422)
    if response.stop_reason == "max_tokens":
        raise IngestError("The plan has too much equipment to read in one pass. Split it into sections.", 422)
    text_out = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if not text_out:
        raise IngestError("Claude returned no result. Try again.", 502)
    try:
        extraction = json.loads(text_out)
    except json.JSONDecodeError:
        raise IngestError("Claude's reply was not valid JSON. Try again.", 502)
    meta = {"model": getattr(response, "model", MODEL), "request_id": getattr(response, "_request_id", None)}
    return extraction, meta
