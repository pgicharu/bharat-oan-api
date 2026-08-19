"""
Soil property lookup via the Beckn BAP search API, wrapping the live
iSDAsoil dataset (pH, organic carbon, texture, nutrients, and more) by
coordinate.

Answers "what's the soil like here" / "what's my soil pH" / "how much
organic carbon does my soil have" questions — distinct from
`knowledge_advisory`'s general agronomy advice and from a Soil Health Card
lookup, which is India-specific and unrelated to this Kenya dataset.

Async ACK + on_search callback, same shape as `agrovets`/`food_prices`
(see `agrovets`'s docstring for the full rationale) — the provider ACKs
the search immediately and posts the actual catalog back to
`/api/bap-webhook/on_search` moments later, so this polls the callback
cache (and, as a fallback, re-POSTs /search) until the catalog arrives or
a timeout is hit.

**No local fallback, unlike agrovets/food_prices.** `isda-soil-provider`
makes a live call to the real iSDAsoil API per request — if that upstream
is down or its credentials aren't configured, the callback still arrives
but with an empty item list and an explicit note in the provider's
`short_desc` (see providers/isda-soil-provider/README.md "Beckn receiver
shim"). `format_output` below surfaces that note verbatim rather than
treating it as a hard failure, since the search itself did succeed — the
upstream soil data just wasn't available this time.
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

# Fixed search parameters. Confirmed working against the live soil-data
# network (2026-08-19) — see providers/isda-soil-provider's
# docs/samples/beckn_search.request.json for the reference envelope.
SOIL_DATA_DOMAIN = "soil-data:oan:kenya"
SOIL_DATA_VERSION = "0.0.1"
SOIL_DATA_COUNTRY = "KEN"
SOIL_DATA_CITY = "std:051"

# iSDAsoil's own property/depth vocabulary — copied from
# providers/isda-soil-provider/isda.py's SOIL_PROPERTIES/SOIL_DEPTHS
# (sourced from the live https://api.isda-africa.com openapi spec on
# 2026-08-18). Kept in sync manually, same convention as `agrovets`'s
# CropCode/ItemCategory. Unrelated to CropCode/GrowthStageCode elsewhere
# in this app — this is soil chemistry, not crop/growth-stage data.
SoilProperty = Literal[
    "aluminium_extractable", "ph", "nitrogen_total", "phosphorous_extractable",
    "potassium_extractable", "magnesium_extractable", "calcium_extractable",
    "iron_extractable", "zinc_extractable", "sulphur_extractable",
    "carbon_total", "carbon_organic", "bulk_density", "bedrock_depth",
    "stone_content", "silt_content", "clay_content", "sand_content",
    "texture_class", "cation_exchange_capacity", "slope_angle", "fcc",
    "land_cover_2015", "crop_cover_2015", "land_cover_2016", "crop_cover_2016",
    "land_cover_2017", "crop_cover_2017", "land_cover_2018", "crop_cover_2018",
    "land_cover_2019", "crop_cover_2019",
]
SoilDepth = Literal["0-20", "0-50", "20-50", "0-200", "0"]

# Rendering cap — omitting `properties` fetches all ~31 available
# properties in one call; this keeps a maximal request from dumping an
# unbounded list into the model's context.
MAX_ITEMS_RENDERED = 20


def _soil_data_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Mirrors `agrovets._agrovets_error` — the agent must never paper over a
    failed lookup with invented soil readings.
    """
    return (
        f"SOIL_DATA_ERROR: {detail} "
        "No soil data was returned by the service. "
        "Tell the farmer plainly that the soil data lookup service did not "
        "respond and that you could not retrieve the information. Do NOT "
        "invent soil property values, and do NOT cite any source. Offer to "
        "help with another farming question instead."
    )


# -----------------------------------------------------------------------
# Request
# -----------------------------------------------------------------------

class SoilDataSearchRequest(BaseModel):
    latitude: float
    longitude: float
    properties: Optional[List[str]] = None
    depth: Optional[str] = None

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        item_tags = []
        if self.properties:
            item_tags.append({"code": "property", "value": ",".join(self.properties)})
        if self.depth:
            item_tags.append({"code": "depth", "value": self.depth})

        item: Dict[str, Any] = {}
        if item_tags:
            item["tags"] = item_tags

        intent: Dict[str, Any] = {
            "fulfillment": {
                "end": {"location": {"gps": f"{self.latitude},{self.longitude}"}}
            }
        }
        if item:
            intent["item"] = item

        return {
            "context": {
                "domain": SOIL_DATA_DOMAIN,
                "country": SOIL_DATA_COUNTRY,
                "city": SOIL_DATA_CITY,
                "action": "search",
                "version": SOIL_DATA_VERSION,
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
# Response models — shaped to providers/isda-soil-provider/beckn.py's
# build_catalog / property_to_beckn output. Tags are flat {code, value}
# pairs, same as agrovets/food_prices.
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

    def __str__(self) -> str:
        label = self.descriptor.name or self.id or "Property"
        value = self.descriptor.short_desc or "no reading"
        parts = [f"- {label}: {value}"]
        depth = _tag_value(self.tags, "depth")
        if depth:
            parts.append(f"depth {depth} cm")
        lower = _tag_value(self.tags, "lower_bound")
        upper = _tag_value(self.tags, "upper_bound")
        if lower and upper:
            parts.append(f"range {lower}–{upper}")
        return " | ".join(parts)


class Address(BaseModel):
    county: Optional[str] = None
    county_name: Optional[str] = None
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

    def __str__(self) -> str:
        lines = [f"**{self.descriptor.name or self.id}**"]
        if self.locations:
            address = self.locations[0].address
            county_name = address.county_name if address else None
            gps = self.locations[0].gps
            loc_bits = [b for b in [county_name, gps] if b]
            if loc_bits:
                lines.append(f"  Location: {' — '.join(loc_bits)}")

        items = self.items or []
        if not items:
            # A live iSDAsoil call failed — the callback still arrives, but
            # empty, with the reason in short_desc (see module docstring).
            if self.descriptor.short_desc:
                lines.append(f"  {self.descriptor.short_desc}")
            else:
                lines.append("  No soil properties were returned.")
            return "\n".join(lines)

        for item in items[:MAX_ITEMS_RENDERED]:
            lines.append(f"  {item}")
        if len(items) > MAX_ITEMS_RENDERED:
            lines.append(f"  (+{len(items) - MAX_ITEMS_RENDERED} more properties — narrow with `properties` to see them)")

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


class SoilDataResponse(BaseModel):
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
            return "No soil data found for that location."

        lines = ["**Soil Property Data**"]
        for provider in providers:
            lines.append(str(provider))

        return "\n\n".join(lines)


# -----------------------------------------------------------------------
# Async ACK + on_search callback handling — same pattern as `agrovets`,
# see that module for the detailed rationale.
# -----------------------------------------------------------------------

def _normalize_soil_data_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Returns (normalized_data, nack_message, is_ack_pending)."""
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Soil data service unavailable. Please try again later."
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


async def _poll_soil_data_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("SOIL_DATA_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("SOIL_DATA_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(search_url, json=poll_payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except httpx.RequestError as err:
            logger.warning("Soil data async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning("Soil data async poll attempt %s returned status %s", attempt, response.status_code)
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Soil data async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_soil_data_result(poll_data)
        if nack_msg:
            logger.warning("Soil data async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_soil_data_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("SOIL_DATA_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("SOIL_DATA_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_soil_data_result(callback_payload)
        if nack_msg:
            logger.warning("Soil data callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:get_soil_data", as_type="tool")
async def get_soil_data(
    latitude: float,
    longitude: float,
    properties: Optional[List[SoilProperty]] = None,
    depth: Optional[SoilDepth] = None,
) -> str:
    """Look up live soil property data (pH, organic carbon, texture, nutrients) for a location in Kenya.

    Use this for "what's the soil like here", "what's my soil pH", "how much
    organic carbon/nitrogen does my soil have", or similar soil-composition
    questions tied to a specific point. Requires coordinates — resolve them
    first via `forward_geocode`, browser location, or the farmer's profile
    Coordinates before calling. Not for general "what should I plant" or
    "how much fertiliser should I use" advice — that's `knowledge_advisory`,
    which can use this tool's numbers as input if the farmer already has them.

    Args:
        latitude: Query point latitude.
        longitude: Query point longitude.
        properties: Specific soil properties to fetch, e.g. ["ph",
            "carbon_organic"]. Omit to fetch every available property (about
            30) in one call — only do this if the farmer asked broadly about
            "my soil" rather than a specific property.
        depth: Soil depth range in cm: "0-20", "0-50", "20-50", "0-200", or
            "0" (surface reading, used for properties like texture_class).
            Omit to use the property's default depth.

    Returns:
        str: Formatted soil property readings for the location, with units
             and the plausible value range iSDAsoil reports for each.
    """
    try:
        request = SoilDataSearchRequest(
            latitude=latitude,
            longitude=longitude,
            properties=properties,
            depth=depth,
        )
        payload = request.get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "soil_data.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _soil_data_error("The soil data service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info("Soil data API search URL: %s", search_url)
        response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        if not response.is_success:
            logger.error(
                "Soil data API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _soil_data_error(f"The soil data service returned HTTP {response.status_code}.")
        logger.info("Soil data API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_soil_data_result(data)
        if nack_msg:
            return _soil_data_error(f"The soil data network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            normalized_data = await _poll_soil_data_callback_cache(transaction_id)
            if normalized_data is None:
                normalized_data = await _poll_soil_data_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Soil data on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _soil_data_error(
                    "The soil data network acknowledged the search but no "
                    "on_search response was received before the timeout."
                )

        if normalized_data is None:
            return _soil_data_error("The soil data service returned an unexpected response format.")

        try:
            soil_data_response = SoilDataResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error("Soil data response failed validation: %s", e)
            return _soil_data_error("The soil data response could not be parsed.")

        rendered = soil_data_response.format_output()
        provider_count = len(soil_data_response._all_providers())
        logger.info("Soil data search rendered %s provider(s) into %s characters", provider_count, len(rendered))
        lf_update_current_observation(
            metadata={
                "tool": "soil_data.search",
                "provider_count": provider_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("Soil data API request timed out")
        return _soil_data_error("The request to the soil data service timed out.")
    except httpx.RequestError as e:
        logger.error("Soil data API request failed: %s", e)
        return _soil_data_error(f"The request to the soil data service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("Soil data request exceeded retry limit")
        return _soil_data_error("The soil data service is temporarily unavailable.")
    except Exception as e:
        logger.error("Error getting soil data: %s", e)
        return _soil_data_error(f"An unexpected error occurred: {str(e)}")
