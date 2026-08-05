"""
Knowledge advisory tool for fetching agronomy advisories via the Beckn BAP search API.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from app.config import DEFAULT_HTTP_TIMEOUT
from app.utils import get_cache
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any, Tuple
from pydantic_ai import UnexpectedModelBehavior
from dotenv import load_dotenv
from langfuse import observe
from helpers.langfuse_tracing import lf_update_current_observation

load_dotenv()

logger = get_logger(__name__)

# Fixed advisory search parameters (see tool docstring).
KA_DOMAIN = "knowledge-advisory:oan:kenya"
KA_TOP_K = "10"
KA_ADVISORY_DOMAIN = "agronomy"
KA_LANGUAGE = "en"

# Tag groups that carry search bookkeeping rather than advisory content, and so
# are not rendered into the agent's tool result.
TAG_CODES_NOT_ADVISORY = {"citation", "retrieval", "classification"}


def _advisory_error(detail: str) -> str:
    """Build an unambiguous tool-failure string for the agent.

    The agent must never paper over an advisory failure with its own background
    knowledge, so every failure path spells that out instead of returning a
    short message the model can read as "nothing to add here".
    """
    return (
        f"KNOWLEDGE_ADVISORY_ERROR: {detail} "
        "No advisory content was returned by the service. "
        "Tell the farmer plainly that the advisory service did not respond and that you "
        "could not retrieve the information. Do NOT answer the question from your own "
        "knowledge, do NOT add general tips or typical dosages, and do NOT cite any source. "
        "Offer to help with another farming question instead."
    )

# -----------------------
# Images
# -----------------------
class Image(BaseModel):
    url: AnyHttpUrl

# -----------------------
# Descriptor
# -----------------------
class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc: Optional[str] = None
    images: Optional[List[Image]] = None

    def __str__(self) -> str:
        if self.name:
            return self.name
        elif self.code:
            return self.code
        return ""

# -----------------------
# Country & Location
# -----------------------
class Country(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None

class City(BaseModel):
    code: Optional[str] = None

class Location(BaseModel):
    country: Optional[Country] = None
    city: Optional[City] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    gps: Optional[str] = None

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
    bap_uri: Optional[AnyHttpUrl] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[AnyHttpUrl] = None
    country: Optional[str] = None
    city: Optional[str] = None
    location: Optional[Location] = None

# -----------------------
# TagItem & Tag
# -----------------------
class TagItem(BaseModel):
    descriptor: Descriptor
    value: str

    def __str__(self) -> str:
        desc_name = self.descriptor.name or self.descriptor.code or "Tag"
        return f"{desc_name}: {self.value}"

class Tag(BaseModel):
    descriptor: Descriptor
    list: List[TagItem]

    def __str__(self) -> str:
        group_name = self.descriptor.name or self.descriptor.code or "Details"
        items_str = "\n      ".join(str(tag_item) for tag_item in self.list)
        return f"{group_name}:\n      {items_str}"

# -----------------------
# Category
# -----------------------
class Category(BaseModel):
    id: str
    descriptor: Descriptor

    def __str__(self) -> str:
        return self.descriptor.name or self.id

# -----------------------
# Item
# -----------------------
class Item(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    matched: Optional[bool] = None
    recommended: Optional[bool] = None
    category_ids: Optional[List[str]] = None
    fulfillment_ids: Optional[List[str]] = None
    tags: Optional[List[Tag]] = None

    def _citation_source(self) -> Optional[str]:
        """Return the source name from the citation tag, if present."""
        for tag in self.tags or []:
            if (tag.descriptor.code or "").lower() == "citation":
                for item in tag.list:
                    if (item.descriptor.code or "").lower() == "source" and item.value:
                        return item.value
        return None

    def _extra_tags(self) -> List[Tag]:
        """Return tags carrying advisory content beyond the citation block.

        The BPP is free to put dosages or other advisory detail in tags rather
        than in long_desc, so anything unrecognised is passed through to the
        agent instead of being silently dropped. Only search bookkeeping is
        skipped: the citation is already rendered as the source line, and
        retrieval scores / classification say nothing a farmer can act on.
        """
        extras = []
        for tag in self.tags or []:
            if (tag.descriptor.code or "").lower() in TAG_CODES_NOT_ADVISORY:
                continue
            if not any(item.value for item in tag.list):
                continue
            extras.append(tag)
        return extras

    def __str__(self) -> str:
        lines = []
        lines.append(f"**Item:** {self.descriptor.name or self.id}")
        if self.descriptor.short_desc:
            lines.append(f"  {self.descriptor.short_desc}")
        if self.descriptor.long_desc:
            lines.append(f"  {self.descriptor.long_desc.strip()}")
        for tag in self._extra_tags():
            tag_str = str(tag).replace("\n", "\n  ")
            lines.append(f"  {tag_str}")
        source = self._citation_source()
        if source:
            lines.append(f"  Source: {source}")
        return "\n".join(lines)

# -----------------------
# Provider
# -----------------------
class Provider(BaseModel):
    id: Optional[str] = None
    descriptor: Descriptor
    categories: Optional[List[Category]] = None
    items: Optional[List[Item]] = None

    def __str__(self) -> str:
        lines = []
        lines.append(f"Provider: {self.descriptor.name or self.id}")
        if self.categories:
            lines.append("  Categories:")
            for cat in self.categories:
                lines.append(f"    - {cat}")
        if self.items:
            lines.append("  Items:")
            for item in self.items:
                item_str = str(item).replace("\n", "\n    ")
                lines.append(f"    {item_str}")
        return "\n".join(lines)

# -----------------------
# Catalog
# -----------------------
class Catalog(BaseModel):
    descriptor: Optional[Descriptor] = None
    providers: Optional[List[Provider]] = None
    items: Optional[List[Item]] = None
    tags: Optional[List[Tag]] = None

    def all_items(self) -> List[Item]:
        items: List[Item] = list(self.items or [])
        for provider in self.providers or []:
            items.extend(provider.items or [])
        return items

    def __str__(self) -> str:
        lines = []
        if self.descriptor:
            lines.append(f"Catalog: {self.descriptor.name or 'N/A'}")
        items = self.all_items()
        if items:
            lines.append("Items:")
            for item in items:
                item_str = str(item).replace("\n", "\n  ")
                lines.append(f"  {item_str}")
        return "\n".join(lines)

# -----------------------
# Message & ResponseItem
# -----------------------
class Message(BaseModel):
    catalog: Catalog

    def __str__(self) -> str:
        return str(self.catalog)

class ResponseItem(BaseModel):
    context: Context
    message: Message

    def __str__(self) -> str:
        return str(self.message)

# -----------------------
# Knowledge Advisory Response
# -----------------------
class KnowledgeAdvisoryResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_advisory_data(self) -> bool:
        for response in self.responses:
            if len(response.message.catalog.all_items()) > 0:
                return True
        return False

    def __str__(self) -> str:
        if len(self.responses) == 0 or not self._has_advisory_data():
            return _advisory_error(
                "The knowledge advisory service replied but the catalog contained no advisory items."
            )

        lines = ["**Knowledge Advisory**"]

        lines.append("Responses:")
        for rsp in self.responses:
            rsp_str = str(rsp).replace("\n", "\n  ")
            lines.append(f"    {rsp_str}")
        return "\n".join(lines)

# -----------------------
# Knowledge Advisory Request
# -----------------------
class KnowledgeAdvisoryRequest(BaseModel):
    """KnowledgeAdvisoryRequest model for the knowledge advisory search API.

    Args:
        query (str): The farmer's question, e.g. "What fertiliser should I use for maize?"
    """
    query: str = Field(..., description="The farmer's question for the knowledge advisory")

    def get_payload(self) -> Dict[str, Any]:
        """Convert the request into the Beckn search payload for knowledge advisory."""
        now = datetime.now(timezone.utc)

        return {
            "context": {
                "domain": KA_DOMAIN,
                "country": "IND",
                "city": "std:080",
                "action": "search",
                "version": "0.0.1",
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
                        "descriptor": {
                            "name": self.query,
                        }
                    },
                    "tags": [
                        {
                            "descriptor": {"code": "topK"},
                            "list": [{"descriptor": {"code": "value"}, "value": KA_TOP_K}],
                        },
                        {
                            "descriptor": {"code": "domain"},
                            "list": [{"descriptor": {"code": "value"}, "value": KA_ADVISORY_DOMAIN}],
                        },
                        {
                            "descriptor": {"code": "language"},
                            "list": [{"descriptor": {"code": "value"}, "value": KA_LANGUAGE}],
                        },
                    ],
                }
            },
        }


def _normalize_advisory_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Normalize Beckn knowledge advisory responses.

    Returns:
        Tuple[normalized_data, nack_message, is_ack_pending]
    """
    if isinstance(data, dict) and "message" in data:
        ack_status = data.get("message", {}).get("ack", {}).get("status")
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Knowledge advisory service unavailable. Please try again later."
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


async def _poll_advisory_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("KA_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("KA_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

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
            logger.warning("Knowledge advisory async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning(
                "Knowledge advisory async poll attempt %s returned status %s",
                attempt,
                response.status_code,
            )
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Knowledge advisory async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_advisory_result(poll_data)
        if nack_msg:
            logger.warning("Knowledge advisory async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_advisory_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("KA_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("KA_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_advisory_result(callback_payload)
        if nack_msg:
            logger.warning("Knowledge advisory callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


@observe(name="tool:knowledge_advisory", as_type="tool")
async def knowledge_advisory(query: str) -> str:
    """Get an agronomy knowledge advisory for a farmer's question.

    This is the ONLY source of agricultural advisory content. Use it for every
    crop, seed, soil, pest, disease, livestock, irrigation, storage, or farming
    practice question — including symptom identification, prevention, and
    treatment (e.g. "early signs of fall armyworm infestation"), which
    fertiliser or variety to use, and general agronomy know-how.

    Args:
        query (str): The farmer's question, e.g. "What fertiliser should I use for maize?"

    Returns:
        str: The knowledge advisory content for the query.
    """
    try:
        payload = KnowledgeAdvisoryRequest(query=query).get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "knowledge_advisory.search",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )
        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return _advisory_error("The advisory service is not configured (BAP_ENDPOINT is not set).")
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Knowledge advisory API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if not response.is_success:
            logger.error(
                "Knowledge advisory API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return _advisory_error(
                f"The advisory service returned HTTP {response.status_code}."
            )
        logger.info("Knowledge advisory API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_advisory_result(data)
        if nack_msg:
            return _advisory_error(f"The advisory network rejected the request: {nack_msg}")

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            # First try the callback payload cache written by /api/bap-webhook/on_search.
            normalized_data = await _poll_advisory_callback_cache(transaction_id)

            # Short polling helps when the network returns ACK immediately but bundles
            # on_search payloads within subsequent /search reads.
            if normalized_data is None:
                normalized_data = await _poll_advisory_async_result(search_url, payload)
            if normalized_data is None:
                logger.error(
                    "Knowledge advisory on_search never arrived for transaction_id %s "
                    "(network ACKed the search but no callback was received)",
                    transaction_id,
                )
                return _advisory_error(
                    "The advisory network acknowledged the search but no on_search response "
                    "was received before the timeout."
                )

        if normalized_data is None:
            return _advisory_error("The advisory service returned an unexpected response format.")

        try:
            advisory_response = KnowledgeAdvisoryResponse.model_validate(normalized_data)
        except Exception as e:
            logger.error(f"Knowledge advisory response failed validation: {e}")
            return _advisory_error("The advisory response could not be parsed.")

        rendered = str(advisory_response)
        item_count = sum(
            len(rsp.message.catalog.all_items()) for rsp in advisory_response.responses
        )
        logger.info(
            "Knowledge advisory rendered %s item(s) into %s characters for the agent",
            item_count,
            len(rendered),
        )
        lf_update_current_observation(
            metadata={
                "tool": "knowledge_advisory.search",
                "item_count": item_count,
                "rendered_chars": len(rendered),
            }
        )
        return rendered

    except httpx.TimeoutException:
        logger.error("Knowledge advisory API request timed out")
        return _advisory_error("The request to the advisory service timed out.")
    except httpx.RequestError as e:
        logger.error(f"Knowledge advisory API request failed: {e}")
        return _advisory_error(f"The request to the advisory service failed: {str(e)}")
    except UnexpectedModelBehavior:
        logger.warning("Knowledge advisory request exceeded retry limit")
        return _advisory_error("The advisory service is temporarily unavailable.")
    except Exception as e:
        logger.error(f"Error getting knowledge advisory: {e}")
        return _advisory_error(f"An unexpected error occurred: {str(e)}")
