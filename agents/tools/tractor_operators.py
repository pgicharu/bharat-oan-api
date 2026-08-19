"""
Tractor operator search via the Beckn BAP search API.

Finds tractor operators in Kenya (Hello Tractor's operator directory) by
implement, tractor brand familiarity, language, region, experience level,
gender, or minimum experience/rating — answering "who can plough my field",
"is there a tractor operator near me", or "who offers a rotavator in my
area" questions.

Async ACK + on_search callback, same shape as `agrovets`/`food_prices`
(see `agrovets`'s docstring for the full rationale) — the provider ACKs
the search immediately and posts the actual catalog back to
`/api/bap-webhook/on_search` moments later, so this polls the callback
cache (and, as a fallback, re-POSTs /search) until the catalog arrives or
a timeout is hit.

**Real, unmasked contact info by the provider's explicit design.** Unlike
`farmer_registry`, `tractor-operators-provider` serves each operator's
real name and phone number unmasked — Hello Tractor already publishes its
operator directory openly, so this republishes already-public data rather
than newly exposing anything (see the provider's README "Data protection
posture"). Nothing in this module adds masking on top of that; results
are rendered as returned.

**No geo filtering.** The source data carries no coordinates — unlike
`agrovets`/`food_prices`, there is no latitude/longitude/radius_km here.
Location narrowing is by the raw `region`/`state` free-text fields only.
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

# Fixed search parameters. Confirmed working against the live
# tractor-operators network (2026-08-19) — see
# providers/tractor-operators-provider's docs/samples/beckn_search.request.json
# for the reference envelope.
TRACTOR_OPERATORS_DOMAIN = "tractor-operators:oan:kenya"
TRACTOR_OPERATORS_VERSION = "0.0.1"
TRACTOR_OPERATORS_COUNTRY = "KEN"
TRACTOR_OPERATORS_CITY = "std:051"

# `experience_level` is the one clean, stable vocabulary value in this
# dataset. `implement`, `familiar_tractor`, `language`, `region`, `state`,
# and `gender` are deliberately left as free text — the source data is
# self-reported with dozens of near-duplicate spelling variants per field
# (e.g. "John Deere" / "John deere" / "john dere") and no canonicalization
# is attempted (see the provider's README "Dataset"), so a Literal here
# would silently reject valid values. The provider does substring/exact
# matching on whatever string it's given.
ExperienceLevel = Literal["JUNIOR", "SENIOR"]

# Rendering caps — a browse-style search could match many operators, each
# with several implements; these keep the tool result bounded.
MAX_OPERATORS_RENDERED = 12
MAX_ITEMS_PER_OPERATOR_RENDERED = 6


def _tractor_operators_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    Mirrors `agrovets._agrovets_error` — the agent must never paper over a
    failed lookup with invented operator names, contacts, or availability.
    """
    return (
        f"TRACTOR_OPERATORS_ERROR: {detail} "
        "No tractor operator data was returned by the service. "
        "Tell the farmer plainly that the tractor operator lookup service "
        "did not respond and that you could not retrieve the information. "
        "Do NOT invent operator names, phone numbers, or availability, and "
        "do NOT cite any source. Offer to help with another farming "
        "question instead."
    )


# -----------------------------------------------------------------------
# Request
# -----------------------------------------------------------------------

class TractorOperatorsSearchRequest(BaseModel):
    implement: Optional[str] = None
    familiar_tractor: Optional[str] = None
    language: Optional[str] = None
    region: Optional[str] = None
    state: Optional[str] = None
    experience_level: Optional[str] = None
    gender: Optional[str] = None
    min_years_experience: Optional[int] = None
    min_star_rating: Optional[float] = None

    def has_item_filter(self) -> bool:
        return bool(
            self.implement or self.familiar_tractor or self.language
            or self.region or self.state or self.experience_level
            or self.gender or self.min_years_experience is not None
            or self.min_star_rating is not None
        )

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        item_tags = []
        if self.familiar_tractor:
            item_tags.append({"code": "familiar_tractor", "value": self.familiar_tractor})
        if self.language:
            item_tags.append({"code": "language", "value": self.language})
        if self.region:
            item_tags.append({"code": "region", "value": self.region})
        if self.state:
            item_tags.append({"code": "state", "value": self.state})
        if self.experience_level:
            item_tags.append({"code": "experience_level", "value": self.experience_level})
        if self.gender:
            item_tags.append({"code": "gender", "value": self.gender})
        if self.min_years_experience is not None:
            item_tags.append({"code": "min_years_experience", "value": str(self.min_years_experience)})
        if self.min_star_rating is not None:
            item_tags.append({"code": "min_star_rating", "value": str(self.min_star_rating)})

        item: Dict[str, Any] = {}
        if self.implement:
            item["descriptor"] = {"name": self.implement}
        if item_tags:
            item["tags"] = item_tags

        intent: Dict[str, Any] = {}
        if item:
            intent["item"] = item

        return {
            "context": {
                "domain": TRACTOR_OPERATORS_DOMAIN,
                "country": TRACTOR_OPERATORS_COUNTRY,
                "city": TRACTOR_OPERATORS_CITY,
                "action": "search",
                "version": TRACTOR_OPERATORS_VERSION,
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
# Response models — shaped to
# providers/tractor-operators-provider/beckn.py's build_catalog /
# operator_to_beckn output. Tags are flat {code, value} pairs, same as
# agrovets/food_prices/soil_data.
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

    @property
    def label(self) -> str:
        return self.descriptor.name or self.id or "Implement"

    def __str__(self) -> str:
        return f"- {self.label}"


class Address(BaseModel):
    region: Optional[str] = None
    state: Optional[str] = None
    lga: Optional[str] = None
    ward: Optional[str] = None
    country: Optional[str] = None


class GeoLocation(BaseModel):
    address: Optional[Address] = None


class Contact(BaseModel):
    phone: Optional[str] = None


class Fulfillment(BaseModel):
    id: Optional[str] = None
    contact: Optional[Contact] = None


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
        parts = [p for p in [address.ward, address.lga, address.region or address.state] if p]
        return ", ".join(parts) if parts else None

    def _phone(self) -> Optional[str]:
        for fulfillment in self.fulfillments or []:
            if fulfillment.contact and fulfillment.contact.phone:
                return fulfillment.contact.phone
        return None

    def __str__(self) -> str:
        lines = [f"**{self.descriptor.name or self.id}**"]
        address_line = self._address_line()
        if address_line:
            lines.append(f"  Location: {address_line}")

        detail_bits = []
        experience_level = _tag_value(self.tags, "experience_level")
        years = _tag_value(self.tags, "years_of_experience")
        if experience_level or years:
            exp_bit = experience_level or ""
            if years and years != "0":
                exp_bit += f" ({years} yr experience)" if exp_bit else f"{years} yr experience"
            if exp_bit:
                detail_bits.append(exp_bit)
        rating = _tag_value(self.tags, "star_rating")
        if rating and rating != "0":
            detail_bits.append(f"{rating}★")
        phone = self._phone()
        if phone:
            detail_bits.append(f"Contact: {phone}")
        if detail_bits:
            lines.append(f"  {' | '.join(detail_bits)}")

        languages = _tag_value(self.tags, "languages")
        if languages:
            lines.append(f"  Languages: {languages}")
        familiar_tractors = _tag_value(self.tags, "familiar_tractors")
        if familiar_tractors:
            lines.append(f"  Familiar with: {familiar_tractors}")

        items = self.items or []
        if items:
            implements = ", ".join(item.label for item in items[:MAX_ITEMS_PER_OPERATOR_RENDERED])
            lines.append(f"  Implements: {implements}")
            if len(items) > MAX_ITEMS_PER_OPERATOR_RENDERED:
                lines.append(f"  (+{len(items) - MAX_ITEMS_PER_OPERATOR_RENDERED} more implements)")

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


class TractorOperatorsResponse(BaseModel):
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
            return "No tractor operators found matching the search."

        lines = ["**Tractor Operator Search**"]
        summary = None
        for rsp in self.responses:
            if rsp.message.catalog.descriptor and rsp.message.catalog.descriptor.short_desc:
                summary = rsp.message.catalog.descriptor.short_desc
                break
        if summary:
            lines.append(summary)

        for provider in providers[:MAX_OPERATORS_RENDERED]:
            lines.append(str(provider))
        if len(providers) > MAX_OPERATORS_RENDERED:
            lines.append(f"(+{len(providers) - MAX_OPERATORS_RENDERED} more operators matched — narrow the search to see them)")

        return "\n\n".join(lines)


# -----------------------------------------------------------------------
# Async ACK + on_search callback handling — same pattern as `agrovets`,
# see that module for the detailed rationale.
# -----------------------------------------------------------------------

def _normalize_tractor_operators_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Returns (normalized_data, nack_message, is_ack_pending)."""
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Tractor operators service unavailable. Please try again later."
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


async def _poll_tractor_operators_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("TRACTOR_OPERATORS_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("TRACTOR_OPERATORS_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

    for attempt in range(1, retry_count + 1):
        await asyncio.sleep(retry_delay_seconds)
        poll_payload = _build_poll_payload(base_payload)
        try:
            response = httpx.post(search_url, json=poll_payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except httpx.RequestError as err:
            logger.warning("Tractor operators async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning("Tractor operators async poll attempt %s returned status %s", attempt, response.status_code)
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Tractor operators async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_tractor_operators_result(poll_data)
        if nack_msg:
            logger.warning("Tractor operators async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_tractor_operators_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("TRACTOR_OPERATORS_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("TRACTOR_OPERATORS_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_tractor_operators_result(callback_payload)
        if nack_msg:
            logger.warning("Tractor operators callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:search_tractor_operators", as_type="tool")
async def search_tractor_operators(
    implement: Optional[str] = None,
    familiar_tractor: Optional[str] = None,
    language: Optional[str] = None,
    region: Optional[str] = None,
    state: Optional[str] = None,
    experience_level: Optional[ExperienceLevel] = None,
    gender: Optional[str] = None,
    min_years_experience: Optional[int] = None,
    min_star_rating: Optional[float] = None,
) -> str:
    """Find available tractor operators in Kenya, by implement, tractor brand, language, or region.

    Use this for "who can plough/rotavate/harrow my field", "tractor
    operator near me", or "who offers a tractor service in my area"
    questions. Returns available operators with contact number, experience,
    languages, and the implements/services they offer. Only operators
    marked available are returned. This is a different dataset from
    `search_agrovets` (agri-input shops) — use this tool only for tractor
    operator/mechanization service questions.

    Args:
        implement: Implement or service name/keyword, e.g. "Plough",
            "Rotavator", "Boom sprayer". Substring match.
        familiar_tractor: Tractor brand the operator is familiar with, e.g.
            "John Deere", "Massey Ferguson". Free text — this field has many
            spelling variants in the source data, pass what the farmer said.
        language: Language the operator speaks, e.g. "English", "Kiswahili".
        region: Region/area name, e.g. "Nandi". Free text, matches the raw
            source field.
        state: State/county-level name. Free text, matches the raw source
            field — may duplicate `region` for some records.
        experience_level: "JUNIOR" or "SENIOR".
        gender: Operator's gender, if the farmer specifically asks for one.
        min_years_experience: Minimum years of experience required.
        min_star_rating: Minimum star rating (0-5) required.

    Returns:
        str: Formatted list of matching, available tractor operators with
             location, experience, contact number, languages, and
             implements/services offered.
    """
    try:
        request = TractorOperatorsSearchRequest(
            implement=implement,
            familiar_tractor=familiar_tractor,
            language=language,
            region=region,
            state=state,
            experience_level=experience_level,
            gender=gender,
            min_years_experience=min_years_experience,
            min_star_rating=min_star_rating,
        )
        # A completely empty search has nothing to filter on, so the network
        # would hand back every operator's record — refuse it here rather
        # than round-tripping for data nobody asked for, the same guard
        # `agrovets`/`food_prices` apply. This is a caller error, not a
        # service failure, so it deliberately doesn't use
        # `_tractor_operators_error`.
        if not request.has_item_filter():
            return (
                "No search criteria given. Call search_tractor_operators again "
                "with at least an implement, tractor brand, language, region, "
                "state, experience level, gender, or a minimum experience/rating "
                "— do not present this as a full operator listing to the farmer."
            )
        payload = request.get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "tractor_operators.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _tractor_operators_error("The tractor operators service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info("Tractor operators API search URL: %s", search_url)
        response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        if not response.is_success:
            logger.error(
                "Tractor operators API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _tractor_operators_error(f"The tractor operators service returned HTTP {response.status_code}.")
        logger.info("Tractor operators API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_tractor_operators_result(data)
        if nack_msg:
            return _tractor_operators_error(f"The tractor operators network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            normalized_data = await _poll_tractor_operators_callback_cache(transaction_id)
            if normalized_data is None:
                normalized_data = await _poll_tractor_operators_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Tractor operators on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _tractor_operators_error(
                    "The tractor operators network acknowledged the search but no "
                    "on_search response was received before the timeout."
                )

        if normalized_data is None:
            return _tractor_operators_error("The tractor operators service returned an unexpected response format.")

        try:
            tractor_operators_response = TractorOperatorsResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error("Tractor operators response failed validation: %s", e)
            return _tractor_operators_error("The tractor operators response could not be parsed.")

        rendered = tractor_operators_response.format_output()
        provider_count = len(tractor_operators_response._all_providers())
        logger.info("Tractor operators search rendered %s provider(s) into %s characters", provider_count, len(rendered))
        lf_update_current_observation(
            metadata={
                "tool": "tractor_operators.search",
                "provider_count": provider_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("Tractor operators API request timed out")
        return _tractor_operators_error("The request to the tractor operators service timed out.")
    except httpx.RequestError as e:
        logger.error("Tractor operators API request failed: %s", e)
        return _tractor_operators_error(f"The request to the tractor operators service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("Tractor operators request exceeded retry limit")
        return _tractor_operators_error("The tractor operators service is temporarily unavailable.")
    except Exception as e:
        logger.error("Error getting tractor operators data: %s", e)
        return _tractor_operators_error(f"An unexpected error occurred: {str(e)}")
