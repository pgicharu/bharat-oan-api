"""
Pesticide/herbicide/fungicide product search via the Beckn BAP search API.

Finds registered crop-protection products in Kenya's Pest Control Products
Board (PCPB) registry by product name, active ingredient, registration
number, manufacturer, pesticide type, or WHO hazard classification —
answering "what can I spray for aphids on tomatoes", "is <product> a
registered pesticide", "what's the WHO hazard class of <product>", or
"which fungicides treat late blight" questions.

Async ACK + on_search callback, same shape as `agrovets`/`food_prices`/
`tractor_operators` (see `agrovets`'s docstring for the full rationale) —
the provider ACKs the search immediately and posts the actual catalog back
to `/api/bap-webhook/on_search` moments later, so this polls the callback
cache (and, as a fallback, re-POSTs /search) until the catalog arrives or
a timeout is hit.

**One provider per product, not a shop.** `pcpb-provider` is a regulatory
registry, not a marketplace — there's no natural "shop" grouping the way
`agrovets`/`food_prices` have one. Each matching registered product comes
back as its own catalog provider with a single item (itself), same shape
as `tractor_operators`'s one-operator-per-provider pattern.

**No geo filtering, no contact info.** This is a public product registry,
not a listing of sellers — there's no location, phone number, or stock
level in the source data. Narrowing is by product/ingredient/registration/
manufacturer/type/hazard-class fields only.

**`pesticide_type`/`who_hazard_class`/`hazard_color_band` are derived, not
verified PCPB columns.** The source registry has no dedicated columns for
these — the provider extracts them from the free-text `registered_uses`
field via keyword/regex heuristics at load time (see
`providers/pcpb-provider/data.py` and its README "Derived fields"). A
product whose free text didn't match gets no value for that facet rather
than a guessed one; render whatever comes back without treating an absent
facet as "unclassified/safe".
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from app.config import DEFAULT_HTTP_TIMEOUT
from app.utils import get_cache
from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Dict, Any, Tuple
from pydantic_ai import UnexpectedModelBehavior
from dotenv import load_dotenv
from langfuse import observe
from helpers.langfuse_tracing import lf_update_current_observation

load_dotenv()

logger = get_logger(__name__)

# Fixed search parameters. Confirmed working against the live pcpb-provider
# network (2026-08-20) — see providers/pcpb-provider's
# docs/samples/beckn_search.request.json for the reference envelope.
PCPB_DOMAIN = "pest-control-products:oan:kenya"
PCPB_VERSION = "0.0.1"
PCPB_COUNTRY = "KEN"
PCPB_CITY = "std:051"

# WHO hazard class and hazard color band are genuinely fixed vocabularies —
# the provider normalizes every extracted value onto one of these (see
# data.py's _WHO_CLASS_NORMALIZE / _COLOR_BAND_RE). pesticide_type is
# likewise matched against a fixed ~18-keyword list (data.py's
# _TYPE_PATTERNS), so it's also safe to enumerate here — unlike
# `tractor_operators`'s free-text fields, none of these are self-reported
# strings with spelling variants.
WhoHazardClass = Literal["Ia", "Ib", "I", "II", "III", "IV", "V", "U"]
HazardColorBand = Literal["Red", "Yellow", "Blue", "Green"]
PesticideType = Literal[
    "Insecticide", "Herbicide", "Fungicide", "Acaricide", "Miticide",
    "Nematicide", "Rodenticide", "Molluscicide", "Bactericide", "Fumigant",
    "Biopesticide", "Plant growth regulator", "Insect growth regulator",
    "Adjuvant", "Surfactant", "Wetter", "Avicide", "Termiticide",
]

# Rendering cap — a broad search (e.g. by pesticide_type alone) could match
# hundreds of products; this keeps the tool result bounded.
MAX_PRODUCTS_RENDERED = 12


def _pcpb_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Mirrors `agrovets._agrovets_error` — the agent must never paper over a
    failed lookup with invented product names, registration numbers, or
    hazard classifications.
    """
    return (
        f"PCPB_ERROR: {detail} "
        "No pesticide product data was returned by the service. "
        "Tell the farmer plainly that the pesticide registry lookup "
        "service did not respond and that you could not retrieve the "
        "information. Do NOT invent product names, registration numbers, "
        "active ingredients, or hazard classifications, and do NOT cite "
        "any source. Offer to help with another farming question instead."
    )


# -----------------------------------------------------------------------
# Request
# -----------------------------------------------------------------------

class PcpbSearchRequest(BaseModel):
    q: Optional[str] = None
    product_name: Optional[str] = None
    active_ingredient: Optional[str] = None
    registration_number: Optional[str] = None
    manufacturer: Optional[str] = None
    pesticide_type: Optional[str] = None
    who_hazard_class: Optional[str] = None
    hazard_color_band: Optional[str] = None

    def has_item_filter(self) -> bool:
        return bool(
            self.q or self.product_name or self.active_ingredient
            or self.registration_number or self.manufacturer
            or self.pesticide_type or self.who_hazard_class
            or self.hazard_color_band
        )

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        item_tags = []
        if self.product_name:
            item_tags.append({"code": "product_name", "value": self.product_name})
        if self.active_ingredient:
            item_tags.append({"code": "active_ingredient", "value": self.active_ingredient})
        if self.registration_number:
            item_tags.append({"code": "registration_number", "value": self.registration_number})
        if self.manufacturer:
            item_tags.append({"code": "manufacturer", "value": self.manufacturer})
        if self.pesticide_type:
            item_tags.append({"code": "pesticide_type", "value": self.pesticide_type})
        if self.who_hazard_class:
            item_tags.append({"code": "who_hazard_class", "value": self.who_hazard_class})
        if self.hazard_color_band:
            item_tags.append({"code": "hazard_color_band", "value": self.hazard_color_band})

        item: Dict[str, Any] = {}
        if self.q:
            item["descriptor"] = {"name": self.q}
        if item_tags:
            item["tags"] = item_tags

        intent: Dict[str, Any] = {}
        if item:
            intent["item"] = item

        return {
            "context": {
                "domain": PCPB_DOMAIN,
                "country": PCPB_COUNTRY,
                "city": PCPB_CITY,
                "action": "search",
                "version": PCPB_VERSION,
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT30S",
            },
            "message": {"intent": intent},
        }


# -----------------------------------------------------------------------
# Response models — shaped to providers/pcpb-provider/beckn.py's
# build_catalog / product_to_beckn output. Tags are flat {code, value}
# pairs, same as agrovets/food_prices/tractor_operators. One provider per
# product, one item per provider (see module docstring).
# -----------------------------------------------------------------------

class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc: Optional[str] = None


class FlatTag(BaseModel):
    code: Optional[str] = None
    value: Optional[str] = None


def _tag_value(tags: Optional[List[FlatTag]], code: str) -> Optional[str]:
    for tag in tags or []:
        if tag.code == code:
            return tag.value
    return None


class Item(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    tags: Optional[List[FlatTag]] = None


class Provider(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    items: Optional[List[Item]] = None
    tags: Optional[List[FlatTag]] = None

    def __str__(self) -> str:
        lines = [f"**{self.descriptor.name or self.id}**"]

        detail_bits = []
        reg_no = _tag_value(self.tags, "registration_number")
        if reg_no:
            detail_bits.append(f"Reg. No: {reg_no}")
        ptype = _tag_value(self.tags, "pesticide_type")
        if ptype:
            detail_bits.append(ptype)
        if detail_bits:
            lines.append(f"  {' | '.join(detail_bits)}")

        active_ingredient = _tag_value(self.tags, "active_ingredient")
        if active_ingredient:
            lines.append(f"  Active ingredient: {active_ingredient}")

        who_class = _tag_value(self.tags, "who_hazard_class")
        color_band = _tag_value(self.tags, "hazard_color_band")
        if who_class or color_band:
            hazard_bits = []
            if who_class:
                hazard_bits.append(f"WHO Class {who_class}")
            if color_band:
                hazard_bits.append(f"{color_band} band")
            lines.append(f"  Hazard: {' — '.join(hazard_bits)}")

        items = self.items or []
        short_desc = items[0].descriptor.short_desc if items and items[0].descriptor else None
        if short_desc:
            lines.append(f"  Registered uses: {short_desc}")

        label_url = _tag_value(self.tags, "label_url")
        if label_url:
            lines.append(f"  Label: {label_url}")

        return "\n".join(lines)


class Catalog(BaseModel):
    descriptor: Optional[Descriptor] = None
    providers: Optional[List[Provider]] = None


class Message(BaseModel):
    catalog: Catalog


class Context(BaseModel):
    ttl: Optional[str] = None
    action: Optional[str] = None
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    domain: Optional[str] = None
    version: Optional[str] = None
    bap_id: Optional[str] = None
    bap_uri: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None


class ResponseItem(BaseModel):
    context: Context
    message: Message


class PcpbResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _all_providers(self) -> List[Provider]:
        providers: List[Provider] = []
        for rsp in self.responses:
            providers.extend(rsp.message.catalog.providers or [])
        return providers

    def format_output(self) -> str:
        providers = self._all_providers()
        if not self.responses or not providers:
            return "No registered pesticide products found matching the search."

        lines = ["**PCPB Registered Product Search**"]
        summary = None
        for rsp in self.responses:
            if rsp.message.catalog.descriptor and rsp.message.catalog.descriptor.short_desc:
                summary = rsp.message.catalog.descriptor.short_desc
                break
        if summary:
            lines.append(summary)

        for provider in providers[:MAX_PRODUCTS_RENDERED]:
            lines.append(str(provider))
        if len(providers) > MAX_PRODUCTS_RENDERED:
            lines.append(f"(+{len(providers) - MAX_PRODUCTS_RENDERED} more products matched — narrow the search to see them)")

        return "\n\n".join(lines)


# -----------------------------------------------------------------------
# Async ACK + on_search callback handling — same pattern as `agrovets`,
# see that module for the detailed rationale.
# -----------------------------------------------------------------------

def _normalize_pcpb_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Returns (normalized_data, nack_message, is_ack_pending)."""
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Pesticide registry service unavailable. Please try again later."
            return None, str(err_msg), False
        if ack_status == "ACK" and "responses" not in data:
            return None, None, True

    if isinstance(data, dict) and "responses" not in data and "context" in data and "message" in data:
        data = {
            "context": data["context"],
            "responses": [{"context": data["context"], "message": data["message"]}],
        }
    return data, None, False


def _build_poll_payload(base_payload: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(base_payload.get("context", {}))
    context["message_id"] = str(uuid.uuid4())
    context["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {"context": context, "message": base_payload.get("message", {})}


async def _poll_pcpb_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("PCPB_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("PCPB_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(search_url, json=poll_payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except httpx.RequestError as err:
            logger.warning("PCPB async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning("PCPB async poll attempt %s returned status %s", attempt, response.status_code)
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("PCPB async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_pcpb_result(poll_data)
        if nack_msg:
            logger.warning("PCPB async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_pcpb_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("PCPB_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("PCPB_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_pcpb_result(callback_payload)
        if nack_msg:
            logger.warning("PCPB callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:search_pesticide_products", as_type="tool")
async def search_pesticide_products(
    q: Optional[str] = None,
    product_name: Optional[str] = None,
    active_ingredient: Optional[str] = None,
    registration_number: Optional[str] = None,
    manufacturer: Optional[str] = None,
    pesticide_type: Optional[PesticideType] = None,
    who_hazard_class: Optional[WhoHazardClass] = None,
    hazard_color_band: Optional[HazardColorBand] = None,
) -> str:
    """Find registered pesticide/herbicide/fungicide products in Kenya's PCPB registry.

    Use this for "what can I spray for <pest> on <crop>", "is <product>
    registered", "what's in <product>", "what's the WHO hazard class of
    <product>", or "which fungicides treat <disease>" questions. Returns
    matching registered products with registration number, active
    ingredient, pesticide type, WHO hazard classification, registered
    uses, and a label link where available. This is a different dataset
    from `search_agrovets` (agri-input shop stock/availability) — use this
    tool for "is this product legally registered and what's it for"
    questions, not "where can I buy it".

    Args:
        q: Broadest filter — free-text substring search across product
            name, active ingredient, AND registered uses (which includes
            target crop/pest names) in one go. Use this for crop/pest
            questions like "late blight" or "aphids on tomatoes", since
            crop and pest are not separate structured fields in this
            registry.
        product_name: Product/trade name keyword, e.g. "Roundup". Substring match.
        active_ingredient: Active ingredient keyword, e.g. "Glyphosate", "Azoxystrobin". Substring match.
        registration_number: PCPB registration number, e.g. "PCPB(CR)1307-P(ii)". Exact match.
        manufacturer: Manufacturer/registrant/local agent keyword. Substring match.
        pesticide_type: One of the fixed product-type categories, e.g. "Fungicide", "Herbicide", "Insecticide".
        who_hazard_class: WHO hazard classification — one of "Ia", "Ib", "I", "II", "III", "IV", "V", "U".
        hazard_color_band: WHO hazard color band — one of "Red", "Yellow", "Blue", "Green".

    Returns:
        str: Formatted list of matching registered products with
             registration number, active ingredient, type, WHO hazard
             class/color band, registered uses, and label link if available.
    """
    try:
        request = PcpbSearchRequest(
            q=q,
            product_name=product_name,
            active_ingredient=active_ingredient,
            registration_number=registration_number,
            manufacturer=manufacturer,
            pesticide_type=pesticide_type,
            who_hazard_class=who_hazard_class,
            hazard_color_band=hazard_color_band,
        )
        # A completely empty search has nothing to filter on, so the network
        # would hand back the entire registry — refuse it here rather than
        # round-tripping for data nobody asked for, the same guard
        # `agrovets`/`food_prices`/`tractor_operators` apply. This is a
        # caller error, not a service failure, so it deliberately doesn't
        # use `_pcpb_error`.
        if not request.has_item_filter():
            return (
                "No search criteria given. Call search_pesticide_products "
                "again with at least a free-text query, product name, "
                "active ingredient, registration number, manufacturer, "
                "pesticide type, WHO hazard class, or hazard color band — "
                "do not present this as a full registry listing to the farmer."
            )
        payload = request.get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "pcpb.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _pcpb_error("The pesticide registry service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info("PCPB API search URL: %s", search_url)
        response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        if not response.is_success:
            logger.error(
                "PCPB API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _pcpb_error(f"The pesticide registry service returned HTTP {response.status_code}.")
        logger.info("PCPB API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_pcpb_result(data)
        if nack_msg:
            return _pcpb_error(f"The pesticide registry network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            normalized_data = await _poll_pcpb_callback_cache(transaction_id)
            if normalized_data is None:
                normalized_data = await _poll_pcpb_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "PCPB on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _pcpb_error(
                    "The pesticide registry network acknowledged the search but no "
                    "on_search response was received before the timeout."
                )

        if normalized_data is None:
            return _pcpb_error("The pesticide registry service returned an unexpected response format.")

        try:
            pcpb_response = PcpbResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error("PCPB response failed validation: %s", e)
            return _pcpb_error("The pesticide registry response could not be parsed.")

        rendered = pcpb_response.format_output()
        provider_count = len(pcpb_response._all_providers())
        logger.info("PCPB search rendered %s product(s) into %s characters", provider_count, len(rendered))
        lf_update_current_observation(
            metadata={
                "tool": "pcpb.search",
                "provider_count": provider_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("PCPB API request timed out")
        return _pcpb_error("The request to the pesticide registry service timed out.")
    except httpx.RequestError as e:
        logger.error("PCPB API request failed: %s", e)
        return _pcpb_error(f"The request to the pesticide registry service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("PCPB request exceeded retry limit")
        return _pcpb_error("The pesticide registry service is temporarily unavailable.")
    except Exception as e:
        logger.error("Error getting PCPB data: %s", e)
        return _pcpb_error(f"An unexpected error occurred: {str(e)}")
