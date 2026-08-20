"""
Farmer registry fetch for enriching advisory context via the Beckn BAP search
API.

Identifier-based lookup, not discovery: the farmer's opaque `farmer_token`
resolves to exactly one profile (county, crops, soil, irrigation).

**Not an LLM-callable tool.** This used to be a `pydantic_ai` tool the agent
decided whether to call — in practice, gpt-4.1-mini (this app's routed
model) reliably skipped it even with explicit prompt instructions to call it
before location-sensitive tools, going straight to `forward_geocode` on
whatever noun was in the query instead. `fetch_farmer_profile_context` is now
called deterministically once per turn from `app.services.chat`, the same way
`moderation_str` is computed before the agent runs, and its rendered text (see
`agents/deps.py::FarmerContext.get_user_message`) is folded straight into the
conversation the model sees — so the model never has to decide to fetch it,
only to read context that is already there.

**Coordinates, not just a county name.** The rendered context also carries a
`Coordinates for location-based tools` line, resolved from the county name via
`agents/tools/kenya_counties.py::county_coordinates` (the same offline Kenya
gazetteer `forward_geocode` already uses) — county names in
`providers/common/counties.py` and this gazetteer are the same underlying
47-county table, so this is a plain dict lookup, not a second geocoding
round-trip. This lets the agent hand `weather_forecast` a lat/lon straight out
of context for "my county"-type phrasing instead of chaining
lookup -> geocode -> weather (a 3-tool chain the model was not reliably
completing).

Tag shape note: unlike weather.py/knowledge_advisory.py, the farmer-registry
BPP uses Beckn's flat tag form (`{"code": ..., "value": ...}`) rather than
the nested descriptor/list tag-group form, on both the search intent and the
on_search catalog item — see providers/common/beckn.py::parse_tags and
providers/farmers-registry/beckn.py::farmer_to_beckn_item.
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
from typing import List, Optional, Dict, Any, Tuple
from pydantic_ai import UnexpectedModelBehavior
from dotenv import load_dotenv
from langfuse import observe
from agents.tools.kenya_counties import county_coordinates
from helpers.langfuse_tracing import lf_update_current_observation

load_dotenv()

logger = get_logger(__name__)

FARMER_REGISTRY_DOMAIN = "farmers-registry:oan:kenya"
FARMER_REGISTRY_VERSION = "0.0.1"
FARMER_REGISTRY_COUNTRY = "KEN"
FARMER_REGISTRY_CITY = "std:051"

NO_PROFILE_MESSAGE = (
    "No farmer profile is on file for this session. Proceed using only what "
    "the farmer tells you directly in the conversation — do not guess their "
    "county, crops, soil, irrigation access, or language."
)

# Tag code -> human label, in display order. Everything else on the item is
# bookkeeping (farmer_token, per-crop code/variety/stage detail) rather than
# a single profile fact, so it is rendered separately.
#
# preferred_language is deliberately excluded: this text is folded straight
# into the prompt (see FarmerContext.get_user_message), right after
# **Selected Language** — which is derived from the request's target_lang and
# is the only thing that should govern response language. Rendering a second,
# differently-labeled "language" line here let the model pick the farmer's
# (often stale/Hindi) registry-stored preference over target_lang, so
# responses — including source citations, per agrinet_en.md's "translate the
# source to match the response language" rule — silently switched language
# even when the frontend explicitly requested English.
_PROFILE_TAG_LABELS: Dict[str, str] = {
    "county_name": "County",
    "sub_county": "Sub-county",
    "ward": "Ward",
    "aez_code": "Agro-ecological zone",
    "soil_type": "Soil type",
    "irrigation": "Irrigation access",
    "channel_preference": "Preferred channel",
    "farm_size_ha": "Farm size (ha)",
    "crop_codes": "Crops",
    "growth_stages": "Growth stages",
}


def _registry_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Farmer profile context silently defaulting to wrong/stale data would bias
    advice (weather for the wrong county, agronomy for the wrong crop) worse
    than admitting the lookup failed, so every failure path says so plainly
    instead of a message the model could read as "no profile, carry on".
    """
    return (
        f"FARMER_REGISTRY_ERROR: {detail} "
        "No farmer profile could be retrieved. Do NOT guess the farmer's "
        "county, crops, soil, irrigation access, or language from this "
        "error — continue using only what the farmer states directly in "
        "the conversation."
    )


# -----------------------
# Descriptor & Location
# -----------------------
class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None


class Country(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None


class City(BaseModel):
    code: Optional[str] = None


class Location(BaseModel):
    country: Optional[Country] = None
    city: Optional[City] = None


# -----------------------
# Context
# -----------------------
class Context(BaseModel):
    ttl: Optional[str] = None
    action: str
    timestamp: str
    message_id: str
    transaction_id: str
    domain: str
    version: str
    bap_id: Optional[str] = None
    bap_uri: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    location: Optional[Location] = None


# -----------------------
# Flat tags (this domain's shape, see module docstring)
# -----------------------
class FlatTag(BaseModel):
    code: str
    value: str


# -----------------------
# Item
# -----------------------
class Item(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    category_id: Optional[str] = None
    tags: Optional[List[FlatTag]] = None

    def _tag_value(self, code: str) -> Optional[str]:
        for tag in self.tags or []:
            if tag.code == code and tag.value:
                return tag.value
        return None

    def _crop_details(self) -> List[str]:
        """Per-crop code/variety@stage detail, one tag per crop — see
        farmers-registry/beckn.py::crop_to_beckn. Kept separate from the
        summary tags above because multiple tags share the "crop_code" code."""
        return [tag.value for tag in (self.tags or []) if tag.code == "crop_code" and tag.value]

    def __str__(self) -> str:
        lines = ["**Farmer Advisory Context**"]
        for code, label in _PROFILE_TAG_LABELS.items():
            value = self._tag_value(code)
            if value:
                lines.append(f"  {label}: {value}")
        crop_details = self._crop_details()
        if crop_details:
            lines.append(f"  Crop detail (code/variety@stage): {', '.join(crop_details)}")
        return "\n".join(lines)


# -----------------------
# Provider & Catalog
# -----------------------
class Provider(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    items: Optional[List[Item]] = None


class Catalog(BaseModel):
    descriptor: Optional[Descriptor] = None
    providers: Optional[List[Provider]] = None

    def all_items(self) -> List[Item]:
        items: List[Item] = []
        for provider in self.providers or []:
            items.extend(provider.items or [])
        return items


# -----------------------
# Message & ResponseItem
# -----------------------
class Message(BaseModel):
    catalog: Catalog


class ResponseItem(BaseModel):
    context: Context
    message: Message


# -----------------------
# Farmer Registry Response
# -----------------------
class FarmerRegistryResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def first_item(self) -> Optional[Item]:
        """A farmer_token lookup resolves to exactly one record — see
        farmers-registry/beckn.py::resolve_farmers — so the first item across
        all responses/providers is the whole answer."""
        for rsp in self.responses:
            items = rsp.message.catalog.all_items()
            if items:
                return items[0]
        return None


# -----------------------
# Farmer Registry Request
# -----------------------
class FarmerLookupRequest(BaseModel):
    farmer_token: str = Field(..., description="Opaque farmer-registry identifier")

    def get_payload(self) -> Dict[str, Any]:
        """Convert the request into the Beckn search payload for the farmer registry."""
        now = datetime.now(timezone.utc)

        return {
            "context": {
                "domain": FARMER_REGISTRY_DOMAIN,
                "country": FARMER_REGISTRY_COUNTRY,
                "city": FARMER_REGISTRY_CITY,
                "action": "search",
                "version": FARMER_REGISTRY_VERSION,
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": now.isoformat(),
                "ttl": "PT30S",
            },
            "message": {
                "intent": {
                    "item": {
                        "tags": [
                            {"code": "farmer_token", "value": self.farmer_token},
                        ]
                    }
                }
            },
        }


def _normalize_registry_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Normalize Beckn farmer-registry responses.

    Returns:
        Tuple[normalized_data, nack_message, is_ack_pending]
    """
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Farmer registry service unavailable. Please try again later."
            return None, str(err_msg), False
        if ack_status == "ACK" and "responses" not in data:
            return None, None, True

    # Adapter may return a single on_search object instead of a responses[] envelope.
    if isinstance(data, dict) and "responses" not in data and "context" in data and "message" in data:
        data = {
            "context": data["context"],
            "responses": [
                {
                    "context": data["context"],
                    "message": data["message"],
                }
            ],
        }
    return data, None, False


def _build_poll_payload(base_payload: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(base_payload.get("context", {}))
    context["message_id"] = str(uuid.uuid4())
    context["timestamp"] = datetime.now(timezone.utc).isoformat()
    return {
        "context": context,
        "message": base_payload.get("message", {}),
    }


async def _poll_registry_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("FARMER_REGISTRY_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("FARMER_REGISTRY_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(
                search_url,
                json=poll_payload,
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
        except httpx.RequestError as err:
            logger.warning("Farmer registry async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning(
                "Farmer registry async poll attempt %s returned status %s",
                attempt,
                response.status_code,
            )
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Farmer registry async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_registry_result(poll_data)
        if nack_msg:
            logger.warning("Farmer registry async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_registry_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("FARMER_REGISTRY_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("FARMER_REGISTRY_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_registry_result(callback_payload)
        if nack_msg:
            logger.warning("Farmer registry callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="context:farmer_profile_fetch", as_type="tool")
async def fetch_farmer_profile_context(
    farmer_token: Optional[str],
) -> Tuple[Optional[str], Optional[Tuple[float, float]]]:
    """Fetch the current farmer's advisory context — county, crops, soil,
    irrigation access, and resolved lat/lon — from the farmer registry, for
    injection into the conversation before the agent runs (see module
    docstring for why this isn't an agent-callable tool).

    Args:
        farmer_token: Opaque farmer-registry identifier for the session, or
            None/empty if no farmer is logged in.

    Returns:
        A `(text, coordinates)` pair:
        - `text` is the rendered advisory context (also embeds a
          "Coordinates for location-based tools" line when it resolves), a
          message indicating no profile is on file, an error string, or
          `None` if `farmer_token` itself was empty. The caller folds
          whichever of these into the conversation as-is (see
          `FarmerContext.farmer_profile_context`).
        - `coordinates` is the same `(lat, lon)` pulled out separately, or
          `None` whenever it didn't resolve — so a caller doing deterministic
          location resolution (see `app.services.chat._resolve_weather_coordinates`)
          doesn't have to re-parse it back out of `text`.
    """
    farmer_token = (farmer_token or "").strip()
    if not farmer_token:
        return None, None

    try:
        payload = FarmerLookupRequest(farmer_token=farmer_token).get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "farmer_registry.lookup",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )
        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _registry_error("The farmer registry service is not configured (BAP_ENDPOINT is not set)."), None
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Farmer registry API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if not response.is_success:
            logger.error(
                "Farmer registry API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _registry_error(f"The farmer registry service returned HTTP {response.status_code}."), None
        logger.info("Farmer registry API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_registry_result(data)
        if nack_msg:
            return _registry_error(f"The farmer registry network rejected the request: {nack_msg}"), None

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            # First try the callback payload cache written by /api/bap-webhook/on_search.
            normalized_data = await _poll_registry_callback_cache(transaction_id)

            # Short polling helps when the network returns ACK immediately but bundles
            # on_search payloads within subsequent /search reads.
            if normalized_data is None:
                normalized_data = await _poll_registry_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Farmer registry on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _registry_error(
                    "The farmer registry network acknowledged the search but no on_search "
                    "response was received before the timeout."
                ), None

        if normalized_data is None:
            return _registry_error("The farmer registry service returned an unexpected response format."), None

        try:
            registry_response = FarmerRegistryResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error(f"Farmer registry response failed validation: {e}")
            return _registry_error("The farmer registry response could not be parsed."), None

        item = registry_response.first_item()
        if item is None:
            logger.info("Farmer registry returned no profile for the given token")
            return NO_PROFILE_MESSAGE, None

        rendered = str(item)
        coords = county_coordinates(item._tag_value("county_name"))
        if coords:
            lat, lon = coords
            rendered += f"\n  Coordinates for location-based tools (weather, mandi, etc.): {lat}, {lon}"
        logger.info("Farmer registry rendered %s characters for the agent", len(rendered))
        lf_update_current_observation(
            metadata={"tool": "farmer_registry.lookup", "rendered_chars": len(rendered)}
        )
        return rendered, coords

    except httpx.TimeoutException:
        logger.error("Farmer registry API request timed out")
        return _registry_error("The request to the farmer registry service timed out."), None
    except httpx.RequestError as e:
        logger.error(f"Farmer registry API request failed: {e}")
        return _registry_error(f"The request to the farmer registry service failed: {str(e)}"), None
    except UnexpectedModelBehavior:
        logger.warning("Farmer registry request exceeded retry limit")
        return _registry_error("The farmer registry service is temporarily unavailable."), None
    except Exception as e:
        logger.error(f"Error getting farmer registry context: {e}")
        return _registry_error(f"An unexpected error occurred: {str(e)}"), None
