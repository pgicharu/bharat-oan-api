"""
Agrovet / agri-input stockist search via the Beckn BAP search API.

Finds agrovets (agri-input shops) in Kenya stocking fertiliser, seed,
pesticide, fungicide, herbicide, veterinary supplies, or equipment, with
price (KES), pack size, stock status, and shop contact/location — answering
"where can I buy X" / "how much does X cost" questions, as opposed to
`knowledge_advisory`'s "what/how much should I apply" questions.

Async ACK + on_search callback, same shape as `knowledge_advisory` (see that
module's docstring for the full rationale) — the provider ACKs the search
immediately and posts the actual catalog back to
`/api/bap-webhook/on_search` moments later, so this polls the callback cache
(and, as a fallback, re-POSTs /search) until the catalog arrives or a
timeout is hit.
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

# Fixed search parameters. Confirmed working against the live agrovets
# network (2026-08-18) — see providers/agrovet-provider's
# docs/samples/beckn_search.request.json for the reference envelope.
AGROVETS_DOMAIN = "agrovets:oan:kenya"
AGROVETS_VERSION = "0.0.1"
AGROVETS_COUNTRY = "KEN"
AGROVETS_CITY = "std:051"

# Controlled vocabulary the agrovet-provider service validates against
# (providers/common/enums.py). Kept in sync manually — this is a small,
# stable list, not worth fetching over the network per call.
CropCode = Literal[
    "maize", "beans", "sorghum", "millet", "rice",
    "wheat", "tea", "coffee", "potato", "cassava",
]
GrowthStageCode = Literal[
    "emergence", "vegetative", "flowering", "grain_fill", "maturity", "post_harvest",
]
ItemCategory = Literal[
    "fertiliser", "seed", "pesticide", "fungicide", "herbicide", "veterinary", "equipment",
]

# Rendering caps — a browse-style search (no item_query/crop/category/growth_stage)
# can match every agrovet in the catalog with its full inventory; these keep the
# tool result from dumping hundreds of items into the model's context.
MAX_PROVIDERS_RENDERED = 12
MAX_ITEMS_PER_PROVIDER_RENDERED = 6


def _agrovets_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Mirrors `knowledge_advisory._advisory_error` — the agent must never
    paper over a failed lookup with invented shop names, prices, or stock
    levels.
    """
    return (
        f"AGROVETS_ERROR: {detail} "
        "No agrovet stock data was returned by the service. "
        "Tell the farmer plainly that the agrovet lookup service did not respond and "
        "that you could not retrieve the information. Do NOT invent shop names, "
        "prices, stock levels, or contact details, and do NOT cite any source. "
        "Offer to help with another farming question instead."
    )


# -----------------------------------------------------------------------
# Request
# -----------------------------------------------------------------------

class AgrovetsSearchRequest(BaseModel):
    item_query: Optional[str] = None
    crop: Optional[str] = None
    category: Optional[str] = None
    growth_stage: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: Optional[float] = None

    def has_item_filter(self) -> bool:
        return bool(self.item_query or self.crop or self.category or self.growth_stage)

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        item_tags = []
        if self.crop:
            item_tags.append({"code": "crop_code", "value": self.crop})
        if self.growth_stage:
            item_tags.append({"code": "growth_stage", "value": self.growth_stage})

        item: Dict[str, Any] = {}
        if self.item_query:
            item["descriptor"] = {"name": self.item_query}
        if item_tags:
            item["tags"] = item_tags

        intent: Dict[str, Any] = {}
        if item:
            intent["item"] = item
        if self.category:
            intent["category"] = {"descriptor": {"code": self.category}}
        if self.latitude is not None and self.longitude is not None:
            intent["fulfillment"] = {
                "end": {"location": {"gps": f"{self.latitude},{self.longitude}"}}
            }
            if self.radius_km is not None:
                intent["tags"] = [{"code": "radius_km", "value": str(self.radius_km)}]

        return {
            "context": {
                "domain": AGROVETS_DOMAIN,
                "country": AGROVETS_COUNTRY,
                "city": AGROVETS_CITY,
                "action": "search",
                "version": AGROVETS_VERSION,
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
# Response models — shaped to providers/agrovet-provider/beckn.py's
# service_point_to_beckn / item_to_beckn output. Tags there are flat
# {code, value} pairs, not the nested Beckn 1.1 tag-group form other tools
# in this codebase use.
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


class Price(BaseModel):
    currency: Optional[str] = None
    value: Optional[str] = None


class Measure(BaseModel):
    unit: Optional[str] = None
    value: Optional[str] = None


class Unitized(BaseModel):
    measure: Optional[Measure] = None


class Available(BaseModel):
    count: Optional[int] = None


class Quantity(BaseModel):
    unitized: Optional[Unitized] = None
    available: Optional[Available] = None


class Item(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    category_id: Optional[str] = None
    price: Optional[Price] = None
    quantity: Optional[Quantity] = None
    tags: Optional[List[FlatTag]] = None

    def __str__(self) -> str:
        label = self.descriptor.short_desc or self.descriptor.name or self.id or "Item"
        parts = [f"- {label}"]
        if self.category_id:
            parts.append(f"[{self.category_id}]")
        if self.price and self.price.value:
            currency = self.price.currency or ""
            parts.append(f"{currency} {self.price.value}".strip())
        in_stock = _tag_value(self.tags, "in_stock")
        if in_stock is not None:
            parts.append("In stock" if in_stock == "true" else "Out of stock")
        active_ingredient = _tag_value(self.tags, "active_ingredient")
        if active_ingredient:
            parts.append(f"active ingredient: {active_ingredient}")
        crop_codes = _tag_value(self.tags, "crop_code")
        if crop_codes:
            parts.append(f"crops: {crop_codes}")
        growth_stages = _tag_value(self.tags, "growth_stage")
        if growth_stages:
            parts.append(f"stage: {growth_stages}")
        return " | ".join(parts)


class Address(BaseModel):
    locality: Optional[str] = None
    ward: Optional[str] = None
    sub_county: Optional[str] = None
    county: Optional[str] = None
    county_name: Optional[str] = None
    country: Optional[str] = None


class GeoLocation(BaseModel):
    id: Optional[str] = None
    gps: Optional[str] = None
    address: Optional[Address] = None


class Contact(BaseModel):
    phone: Optional[str] = None


class FulfillmentTime(BaseModel):
    days: Optional[str] = None


class Stop(BaseModel):
    type: Optional[str] = None
    location: Optional[GeoLocation] = None
    time: Optional[FulfillmentTime] = None


class Fulfillment(BaseModel):
    id: Optional[str] = None
    type: Optional[str] = None
    contact: Optional[Contact] = None
    stops: Optional[List[Stop]] = None


class Provider(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    locations: Optional[List[GeoLocation]] = None
    fulfillments: Optional[List[Fulfillment]] = None
    items: Optional[List[Item]] = None
    tags: Optional[List[FlatTag]] = None

    def _address_line(self) -> Optional[str]:
        if not self.locations:
            return None
        address = self.locations[0].address
        if not address:
            return None
        parts = [p for p in [address.ward, address.sub_county, address.county_name] if p]
        return ", ".join(parts) if parts else None

    def _phone(self) -> Optional[str]:
        for fulfillment in self.fulfillments or []:
            if fulfillment.contact and fulfillment.contact.phone:
                return fulfillment.contact.phone
        return None

    def _hours(self) -> Optional[str]:
        for fulfillment in self.fulfillments or []:
            for stop in fulfillment.stops or []:
                if stop.time and stop.time.days:
                    return stop.time.days
        return None

    def _delivery(self) -> Optional[str]:
        delivery_available = _tag_value(self.tags, "delivery_available")
        if delivery_available == "true":
            return "Delivery available"
        if delivery_available == "false":
            return "Pickup only"
        return None

    def __str__(self) -> str:
        lines = [f"**{self.descriptor.name or self.id}**"]
        if self.descriptor.short_desc:
            lines.append(f"  {self.descriptor.short_desc}")
        address_line = self._address_line()
        distance_km = _tag_value(self.tags, "distance_km")
        if address_line or distance_km:
            loc_bits = [b for b in [address_line, f"{distance_km} km away" if distance_km else None] if b]
            lines.append(f"  Location: {' — '.join(loc_bits)}")
        contact_bits = [b for b in [self._phone(), self._delivery(), self._hours()] if b]
        if contact_bits:
            lines.append(f"  Contact: {' | '.join(contact_bits)}")
        licence_number = _tag_value(self.tags, "licence_number")
        if licence_number:
            lines.append(f"  Licence: {licence_number}")

        items = self.items or []
        for item in items[:MAX_ITEMS_PER_PROVIDER_RENDERED]:
            lines.append(f"  {item}")
        if len(items) > MAX_ITEMS_PER_PROVIDER_RENDERED:
            lines.append(f"  (+{len(items) - MAX_ITEMS_PER_PROVIDER_RENDERED} more items at this agrovet)")

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


class AgrovetsResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _all_providers(self) -> List[Provider]:
        providers: List[Provider] = []
        for rsp in self.responses:
            providers.extend(rsp.message.catalog.providers or [])
        return providers

    def _has_data(self) -> bool:
        return any(p.items for p in self._all_providers()) or bool(self._all_providers())

    def format_output(self) -> str:
        providers = self._all_providers()
        if not self.responses or not providers:
            return "No agrovets found matching the search."

        lines = ["**Agrovet / Agri-Input Search**"]
        summary = None
        for rsp in self.responses:
            if rsp.message.catalog.descriptor and rsp.message.catalog.descriptor.short_desc:
                summary = rsp.message.catalog.descriptor.short_desc
                break
        if summary:
            lines.append(summary)

        for provider in providers[:MAX_PROVIDERS_RENDERED]:
            lines.append(str(provider))
        if len(providers) > MAX_PROVIDERS_RENDERED:
            lines.append(f"(+{len(providers) - MAX_PROVIDERS_RENDERED} more agrovets matched — narrow the search to see them)")

        return "\n\n".join(lines)


# -----------------------------------------------------------------------
# Async ACK + on_search callback handling — same pattern as
# `knowledge_advisory`, see that module for the detailed rationale.
# -----------------------------------------------------------------------

def _normalize_agrovets_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Returns (normalized_data, nack_message, is_ack_pending)."""
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Agrovets service unavailable. Please try again later."
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


async def _poll_agrovets_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("AGROVETS_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("AGROVETS_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(search_url, json=poll_payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except httpx.RequestError as err:
            logger.warning("Agrovets async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning("Agrovets async poll attempt %s returned status %s", attempt, response.status_code)
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Agrovets async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_agrovets_result(poll_data)
        if nack_msg:
            logger.warning("Agrovets async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_agrovets_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("AGROVETS_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("AGROVETS_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_agrovets_result(callback_payload)
        if nack_msg:
            logger.warning("Agrovets callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:search_agrovets", as_type="tool")
async def search_agrovets(
    item_query: Optional[str] = None,
    crop: Optional[CropCode] = None,
    category: Optional[ItemCategory] = None,
    growth_stage: Optional[GrowthStageCode] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius_km: Optional[float] = None,
) -> str:
    """Find agrovets (agri-input shops) in Kenya stocking a product, with price and stock.

    Use this for "where can I buy X", "which agrovet sells X", or "how much does X
    cost" questions. Returns matching agrovets with price (KES), pack size, stock
    status, and shop contact/location. For "what should I use" or "how much should I
    apply" questions, use `knowledge_advisory` instead — this tool is for where to
    buy an input, not agronomic advice about it.

    Args:
        item_query: Product name or keyword, e.g. "CAN", "DAP", "glyphosate".
        crop: Narrow to inputs for one crop: maize, beans, sorghum, millet, rice,
            wheat, tea, coffee, potato, or cassava.
        category: Narrow to one input type: fertiliser, seed, pesticide, fungicide,
            herbicide, veterinary, or equipment.
        growth_stage: Narrow to one growth stage: emergence, vegetative, flowering,
            grain_fill, maturity, or post_harvest.
        latitude: Farmer's latitude — sorts results by distance nearest-first.
        longitude: Farmer's longitude — sorts results by distance nearest-first.
        radius_km: Maximum distance in km from latitude/longitude. Only takes effect
            when both latitude and longitude are also given.

    Returns:
        str: Formatted list of matching agrovets with stocked items, prices, stock
             status, and shop contact/location details.
    """
    try:
        request = AgrovetsSearchRequest(
            item_query=item_query,
            crop=crop,
            category=category,
            growth_stage=growth_stage,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
        )
        # A completely empty search has nothing to filter on, so the network
        # would hand back an unfiltered browse of the whole agrovets catalog
        # (the same silent-over-disclosure the farmers-registry bug report
        # flagged and fixed) — refuse it here rather than round-tripping for
        # data nobody asked for. This is a caller error, not a service
        # failure, so it deliberately doesn't use `_agrovets_error`.
        if not request.has_item_filter() and request.latitude is None and request.longitude is None:
            return (
                "No search criteria given. Call search_agrovets again with at least a "
                "product/keyword (item_query), crop, category, or the farmer's location "
                "— do not present this as a full agrovet listing to the farmer."
            )
        payload = request.get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "agrovets.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _agrovets_error("The agrovets service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info("Agrovets API search URL: %s", search_url)
        response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        if not response.is_success:
            logger.error(
                "Agrovets API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _agrovets_error(f"The agrovets service returned HTTP {response.status_code}.")
        logger.info("Agrovets API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_agrovets_result(data)
        if nack_msg:
            return _agrovets_error(f"The agrovets network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            normalized_data = await _poll_agrovets_callback_cache(transaction_id)
            if normalized_data is None:
                normalized_data = await _poll_agrovets_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Agrovets on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _agrovets_error(
                    "The agrovets network acknowledged the search but no on_search response "
                    "was received before the timeout."
                )

        if normalized_data is None:
            return _agrovets_error("The agrovets service returned an unexpected response format.")

        try:
            agrovets_response = AgrovetsResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error("Agrovets response failed validation: %s", e)
            return _agrovets_error("The agrovets response could not be parsed.")

        rendered = agrovets_response.format_output()
        provider_count = len(agrovets_response._all_providers())
        logger.info("Agrovets search rendered %s provider(s) into %s characters", provider_count, len(rendered))
        lf_update_current_observation(
            metadata={
                "tool": "agrovets.search",
                "provider_count": provider_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("Agrovets API request timed out")
        return _agrovets_error("The request to the agrovets service timed out.")
    except httpx.RequestError as e:
        logger.error("Agrovets API request failed: %s", e)
        return _agrovets_error(f"The request to the agrovets service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("Agrovets request exceeded retry limit")
        return _agrovets_error("The agrovets service is temporarily unavailable.")
    except Exception as e:
        logger.error("Error getting agrovets data: %s", e)
        return _agrovets_error(f"An unexpected error occurred: {str(e)}")
