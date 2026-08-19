"""
Food/commodity market price search via the Beckn BAP search API.

Finds WFP/HDX food price observations in Kenya by market, commodity,
category, price type, or geo-proximity — answering "what does X cost in
Y market" questions, as opposed to `mandi`'s India mandi prices or
`agrovets`'s agri-input shop prices.

Async ACK + on_search callback, same shape as `agrovets` (see that
module's docstring for the full rationale) — the provider ACKs the search
immediately and posts the actual catalog back to
`/api/bap-webhook/on_search` moments later, so this polls the callback
cache (and, as a fallback, re-POSTs /search) until the catalog arrives or
a timeout is hit.
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

# Fixed search parameters. Confirmed working against the live food-prices
# network (2026-08-19) — see providers/food-prices-provider's
# docs/samples/beckn_search.request.json for the reference envelope.
FOOD_PRICES_DOMAIN = "food-prices:oan:kenya"
FOOD_PRICES_VERSION = "0.0.1"
FOOD_PRICES_COUNTRY = "KEN"
FOOD_PRICES_CITY = "std:051"

# Small, stable controlled vocabulary the food-prices-provider validates
# against (its `common/enums.py`-equivalent category list). `commodity` and
# `market`/`admin1`/`admin2` are deliberately left as free text — the real
# dataset has 51 commodities and 226 markets with no stable name-based
# crosswalk onto this repo's county system (see the provider's README §
# "Dataset"), so the provider does its own substring/exact matching.
PriceCategory = Literal[
    "cereals and tubers", "pulses and nuts", "vegetables and fruits",
    "meat, fish and eggs", "milk and dairy", "oil and fats",
    "miscellaneous food", "non-food",
]
PriceType = Literal["Retail", "Wholesale"]

# Rendering caps — a browse-style search (no commodity/market/admin filter)
# can match every market in the catalog; these keep the tool result from
# dumping hundreds of price rows into the model's context.
MAX_MARKETS_RENDERED = 12
MAX_ITEMS_PER_MARKET_RENDERED = 8


def _food_prices_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Mirrors `agrovets._agrovets_error` — the agent must never paper over a
    failed lookup with invented markets, commodities, or prices.
    """
    return (
        f"FOOD_PRICES_ERROR: {detail} "
        "No food price data was returned by the service. "
        "Tell the farmer plainly that the food prices lookup service did not "
        "respond and that you could not retrieve the information. Do NOT "
        "invent market names, commodities, or prices, and do NOT cite any "
        "source. Offer to help with another farming question instead."
    )


# -----------------------------------------------------------------------
# Request
# -----------------------------------------------------------------------

class FoodPricesSearchRequest(BaseModel):
    commodity: Optional[str] = None
    market: Optional[str] = None
    admin1: Optional[str] = None
    admin2: Optional[str] = None
    category: Optional[str] = None
    pricetype: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_km: Optional[float] = None

    def has_item_filter(self) -> bool:
        return bool(
            self.commodity or self.market or self.admin1 or self.admin2
            or self.category or self.pricetype
        )

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        item_tags = []
        if self.market:
            item_tags.append({"code": "market", "value": self.market})
        if self.admin1:
            item_tags.append({"code": "admin1", "value": self.admin1})
        if self.admin2:
            item_tags.append({"code": "admin2", "value": self.admin2})
        if self.pricetype:
            item_tags.append({"code": "pricetype", "value": self.pricetype})

        item: Dict[str, Any] = {}
        if self.commodity:
            item["descriptor"] = {"name": self.commodity}
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
                "domain": FOOD_PRICES_DOMAIN,
                "country": FOOD_PRICES_COUNTRY,
                "city": FOOD_PRICES_CITY,
                "action": "search",
                "version": FOOD_PRICES_VERSION,
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
# Response models — shaped to providers/food-prices-provider/beckn.py's
# build_catalog / item_to_beckn output. Tags are flat {code, value} pairs,
# same as agrovets.
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


class Item(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    category_id: Optional[str] = None
    price: Optional[Price] = None
    tags: Optional[List[FlatTag]] = None

    def __str__(self) -> str:
        label = self.descriptor.name or self.id or "Item"
        parts = [f"- {label}"]
        unit = _tag_value(self.tags, "unit")
        if self.price and self.price.value:
            currency = self.price.currency or ""
            price_bit = f"{currency} {self.price.value}".strip()
            if unit:
                price_bit += f" / {unit}"
            parts.append(price_bit)
        pricetype = _tag_value(self.tags, "pricetype")
        if pricetype:
            parts.append(pricetype)
        date = _tag_value(self.tags, "date")
        if date:
            parts.append(f"as of {date}")
        return " | ".join(parts)


class Address(BaseModel):
    admin1: Optional[str] = None
    admin2: Optional[str] = None
    country: Optional[str] = None


class GeoLocation(BaseModel):
    id: Optional[str] = None
    gps: Optional[str] = None
    address: Optional[Address] = None


class Provider(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    locations: Optional[List[GeoLocation]] = None
    items: Optional[List[Item]] = None
    tags: Optional[List[FlatTag]] = None

    def _address_line(self) -> Optional[str]:
        if not self.locations:
            return None
        address = self.locations[0].address
        if not address:
            return None
        parts = [p for p in [address.admin2, address.admin1] if p]
        return ", ".join(parts) if parts else None

    def __str__(self) -> str:
        lines = [f"**{self.descriptor.name or self.id}**"]
        address_line = self._address_line()
        distance_km = _tag_value(self.tags, "distance_km")
        if address_line or distance_km:
            loc_bits = [b for b in [address_line, f"{distance_km} km away" if distance_km else None] if b]
            lines.append(f"  Location: {' — '.join(loc_bits)}")

        items = self.items or []
        for item in items[:MAX_ITEMS_PER_MARKET_RENDERED]:
            lines.append(f"  {item}")
        if len(items) > MAX_ITEMS_PER_MARKET_RENDERED:
            lines.append(f"  (+{len(items) - MAX_ITEMS_PER_MARKET_RENDERED} more commodities at this market)")

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


class FoodPricesResponse(BaseModel):
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
            return "No food price data found matching the search."

        lines = ["**Food/Commodity Market Prices**"]
        summary = None
        for rsp in self.responses:
            if rsp.message.catalog.descriptor and rsp.message.catalog.descriptor.short_desc:
                summary = rsp.message.catalog.descriptor.short_desc
                break
        if summary:
            lines.append(summary)

        for provider in providers[:MAX_MARKETS_RENDERED]:
            lines.append(str(provider))
        if len(providers) > MAX_MARKETS_RENDERED:
            lines.append(f"(+{len(providers) - MAX_MARKETS_RENDERED} more markets matched — narrow the search to see them)")

        return "\n\n".join(lines)


# -----------------------------------------------------------------------
# Async ACK + on_search callback handling — same pattern as `agrovets`,
# see that module for the detailed rationale.
# -----------------------------------------------------------------------

def _normalize_food_prices_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Returns (normalized_data, nack_message, is_ack_pending)."""
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Food prices service unavailable. Please try again later."
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


async def _poll_food_prices_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("FOOD_PRICES_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("FOOD_PRICES_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(search_url, json=poll_payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except httpx.RequestError as err:
            logger.warning("Food prices async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning("Food prices async poll attempt %s returned status %s", attempt, response.status_code)
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Food prices async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_food_prices_result(poll_data)
        if nack_msg:
            logger.warning("Food prices async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_food_prices_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("FOOD_PRICES_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("FOOD_PRICES_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_food_prices_result(callback_payload)
        if nack_msg:
            logger.warning("Food prices callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:search_food_prices", as_type="tool")
async def search_food_prices(
    commodity: Optional[str] = None,
    market: Optional[str] = None,
    admin1: Optional[str] = None,
    admin2: Optional[str] = None,
    category: Optional[PriceCategory] = None,
    pricetype: Optional[PriceType] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius_km: Optional[float] = None,
) -> str:
    """Find current food/commodity market prices in Kenya, by commodity, market, or location.

    Use this for "what does X cost", "price of X in Y market", or "commodity
    prices near me" questions about food staples and other tracked commodities.
    Returns the latest price per (market, commodity, price type) match, in
    KES. This is a different dataset from `search_agrovets` (agri-input shop
    stock/prices, e.g. fertiliser or seed) and from `get_mandi_prices` (India
    mandi prices) — use this tool only for Kenya food/commodity market prices.

    Args:
        commodity: Commodity name or keyword, e.g. "Maize", "Beans", "Cooking
            fat". Case-insensitive substring match — "Maize" also matches
            "Maize (white)".
        market: Exact market name, e.g. "Mombasa", "Nairobi".
        admin1: Province-level region name (WFP's own pre-2013 provincial
            names, e.g. "Rift Valley", "Coast" — not this app's 47-county
            system).
        admin2: District-level region name (WFP's own pre-2013 district
            names, e.g. "Nakuru", "Mombasa").
        category: Narrow to one commodity category: "cereals and tubers",
            "pulses and nuts", "vegetables and fruits", "meat, fish and eggs",
            "milk and dairy", "oil and fats", "miscellaneous food", or
            "non-food".
        pricetype: "Retail" or "Wholesale".
        latitude: Query point latitude — sorts results by distance to the
            nearest matching market.
        longitude: Query point longitude — sorts results by distance to the
            nearest matching market.
        radius_km: Maximum distance in km from latitude/longitude. Only takes
            effect when both latitude and longitude are also given.

    Returns:
        str: Formatted list of matching markets with priced commodities,
             price (KES), price type, and observation date.
    """
    try:
        request = FoodPricesSearchRequest(
            commodity=commodity,
            market=market,
            admin1=admin1,
            admin2=admin2,
            category=category,
            pricetype=pricetype,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
        )
        # A completely empty search has nothing to filter on, so the network
        # would hand back an unfiltered browse of the whole food-prices
        # catalog — refuse it here rather than round-tripping for data
        # nobody asked for. This is a caller error, not a service failure,
        # so it deliberately doesn't use `_food_prices_error`.
        if not request.has_item_filter() and request.latitude is None and request.longitude is None:
            return (
                "No search criteria given. Call search_food_prices again with at "
                "least a commodity, market, admin1/admin2 region, category, "
                "price type, or a location — do not present this as a full "
                "price listing to the farmer."
            )
        payload = request.get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "food_prices.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _food_prices_error("The food prices service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info("Food prices API search URL: %s", search_url)
        response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        if not response.is_success:
            logger.error(
                "Food prices API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _food_prices_error(f"The food prices service returned HTTP {response.status_code}.")
        logger.info("Food prices API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_food_prices_result(data)
        if nack_msg:
            return _food_prices_error(f"The food prices network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            normalized_data = await _poll_food_prices_callback_cache(transaction_id)
            if normalized_data is None:
                normalized_data = await _poll_food_prices_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Food prices on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _food_prices_error(
                    "The food prices network acknowledged the search but no "
                    "on_search response was received before the timeout."
                )

        if normalized_data is None:
            return _food_prices_error("The food prices service returned an unexpected response format.")

        try:
            food_prices_response = FoodPricesResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error("Food prices response failed validation: %s", e)
            return _food_prices_error("The food prices response could not be parsed.")

        rendered = food_prices_response.format_output()
        provider_count = len(food_prices_response._all_providers())
        logger.info("Food prices search rendered %s market(s) into %s characters", provider_count, len(rendered))
        lf_update_current_observation(
            metadata={
                "tool": "food_prices.search",
                "provider_count": provider_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("Food prices API request timed out")
        return _food_prices_error("The request to the food prices service timed out.")
    except httpx.RequestError as e:
        logger.error("Food prices API request failed: %s", e)
        return _food_prices_error(f"The request to the food prices service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("Food prices request exceeded retry limit")
        return _food_prices_error("The food prices service is temporarily unavailable.")
    except Exception as e:
        logger.error("Error getting food prices data: %s", e)
        return _food_prices_error(f"An unexpected error occurred: {str(e)}")
