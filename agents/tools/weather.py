"""
Weather tool for fetching weather forecast data via the Beckn BAP search API.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from helpers.utils import get_logger, get_today_date_str
import httpx
from app.config import DEFAULT_HTTP_TIMEOUT
from app.utils import get_cache
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any, Tuple
from dateutil import parser
from dateutil.parser import ParserError
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from dotenv import load_dotenv
from langfuse import observe
from helpers.langfuse_tracing import lf_update_current_observation

load_dotenv()

logger = get_logger(__name__)

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

    def is_date(self) -> Tuple[bool, Optional[datetime]]:
        """Check if the descriptor code or name contains a parseable date.
        
        Returns:
            Tuple[bool, Optional[datetime]]: (True, datetime_obj) if date found, (False, None) if not
        """
        try:
            # Try code first as it's more likely to contain the date
            if self.code:
                return True, parser.parse(self.code, fuzzy=True)
            # Try name if code didn't work
            if self.name:
                return True, parser.parse(self.name, fuzzy=True)
            return False, None
        except (ParserError, TypeError, ValueError):
            return False, None

    def __str__(self) -> str:
        """Return the 'name' or 'code' if present, else empty."""
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
    # Mark optional if not always present
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
        """Example format:
           TagGroupName:
               TagItem1
               TagItem2
        """
        group_name = self.descriptor.name or self.descriptor.code or "Details"
        items_str = "\n      ".join(str(tag_item) for tag_item in self.list)
        return f"{group_name}:\n      {items_str}"

# -----------------------
# Stop & Fulfillment
# -----------------------
class Stop(BaseModel):
    location: Optional[Location] = None

class Fulfillment(BaseModel):
    id: Optional[str] = None
    stops: Optional[List[Stop]] = None

    def __str__(self) -> str:
        lines = [f"Fulfillment ID: {self.id}"]
        if self.stops:
            lines.append("  Stops:")
            for stop in self.stops:
                if not stop.location:
                    continue
                if stop.location.gps:
                    lines.append(f"    - GPS: {stop.location.gps}")
                elif stop.location.lat and stop.location.lon:
                    lines.append(f"    - Lat: {stop.location.lat}, Lon: {stop.location.lon}")
        return "\n".join(lines)

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
    id: str
    descriptor: Descriptor
    matched: bool
    recommended: bool
    category_ids: Optional[List[str]] = None
    fulfillment_ids: Optional[List[str]] = None
    tags: Optional[List[Tag]] = None

    def __str__(self) -> str:
        lines = []
        # Item name / ID heading
        lines.append(f"**Item:** {self.descriptor.name or self.id}")

        # Short/Long
        if self.descriptor.short_desc:
            lines.append(f"  Short: {self.descriptor.short_desc}")
        if self.descriptor.long_desc:
            # strip() to remove trailing newlines
            lines.append(f"  Long: {self.descriptor.long_desc.strip()}")

        # Show tags
        if self.tags:
            lines.append("  Tags:")
            for t in self.tags:
                tag_str = str(t).replace("\n", "\n    ")
                lines.append(f"    {tag_str}")

        return "\n".join(lines)

# -----------------------
# Provider
# -----------------------
class Provider(BaseModel):
    id: str
    descriptor: Descriptor
    categories: Optional[List[Category]] = None
    fulfillments: Optional[List[Fulfillment]] = None
    items: Optional[List[Item]] = None

    def __str__(self) -> str:
        lines = []
        lines.append(f"Provider: {self.descriptor.name or self.id}")

        if self.categories:
            lines.append("  Categories:")
            for cat in self.categories:
                lines.append(f"    - {cat}")

        if self.fulfillments:
            lines.append("  Fulfillments:")
            for f in self.fulfillments:
                f_str = str(f).replace("\n", "\n    ")
                lines.append(f"    {f_str}")

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
    descriptor: Descriptor
    providers: List[Provider]

    def __str__(self) -> str:
        lines = []
        lines.append(f"Catalog: {self.descriptor.name or 'N/A'}")
        if self.providers:
            lines.append("Providers:")
            for provider in self.providers:
                provider_str = str(provider).replace("\n", "\n  ")
                lines.append(f"  {provider_str}")
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
# Weather Response
# -----------------------
class WeatherResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_weather_data(self) -> bool:
        """Check if there are any responses with providers that have items."""
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if provider.items and len(provider.items) > 0:
                    return True
        return False
    
    def __str__(self) -> str:
        lines = []
        
        lines.append(f"**Weather Forecast Data** [Today's Date: {get_today_date_str()}]")
        no_data_message = "No weather forecast data found for the requested location."
    
        # Check if there are any responses with providers that have items
        has_weather_data = self._has_weather_data()
        if len(self.responses) == 0 or not has_weather_data:
            lines.append(no_data_message)
            return "\n".join(lines)
        else:
            lines.append("Responses:")
            for idx, rsp in enumerate(self.responses, start=1):
                rsp_str = str(rsp).replace("\n", "\n  ")
                lines.append(f"    {rsp_str}")
            return "\n".join(lines)

# -----------------------
# Weather Request
# -----------------------
class WeatherRequest(BaseModel):
    """WeatherRequest model for weather forecast API.
    
    Args:
        latitude (float): Latitude of the location, example: 12.9716
        longitude (float): Longitude of the location, example: 77.5946
    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the WeatherRequest object to a dictionary compatible with Vistaar Beckn API.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the request payload.
        """
        now = datetime.now(timezone.utc)
        
        return {
            "context": {
                "domain": "weather-forecast:oan:kenya",
                "action": "search",
                "version": "0.0.1",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                # "bpp_id": os.getenv("BPP_ID"),
                # "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": now.isoformat(),
                "ttl": "PT10M",
                "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "std:080"
                    }
                }
            },
            "message": {
                "intent": {
                    "category": {
                        "descriptor": {
                            "name": "Weather-Forecast-Mausamgram",
                            "code": "WFC"
                        }
                    },
                    "fulfillment": {
                        "stops": [
                            {
                                "location": {
                                    "gps": f"{self.latitude},{self.longitude}"
                                }
                            }
                        ]
                    }
                }
            }
        }


def _normalize_weather_result(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Normalize Beckn weather responses.

    Returns:
        Tuple[normalized_data, nack_message, is_ack_pending]
    """
    if isinstance(data, dict) and "message" in data:
        ack_status = (
            data.get("message", {})
            .get("ack", {})
            .get("status")
        )
        if ack_status == "NACK":
            err = data.get("message", {}).get("error", {})
            err_msg = err.get("message") or "Weather service unavailable. Please try again later."
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


async def _poll_weather_async_result(search_url: str, base_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    retry_count = max(int(os.getenv("WEATHER_ASYNC_RETRY_COUNT", "3")), 0)
    retry_delay_seconds = max(float(os.getenv("WEATHER_ASYNC_RETRY_DELAY_SECONDS", "2")), 0)

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
            logger.warning("Weather async poll attempt %s failed: %s", attempt, err)
            continue

        if not response.is_success:
            logger.warning(
                "Weather async poll attempt %s returned status %s",
                attempt,
                response.status_code,
            )
            continue

        try:
            poll_data = response.json()
        except ValueError:
            logger.warning("Weather async poll attempt %s returned non-JSON response", attempt)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_weather_result(poll_data)
        if nack_msg:
            logger.warning("Weather async poll returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None


async def _poll_weather_callback_cache(transaction_id: str | None) -> Optional[Dict[str, Any]]:
    if not transaction_id:
        return None

    retry_count = max(int(os.getenv("WEATHER_CALLBACK_RETRY_COUNT", "12")), 0)
    retry_delay_seconds = max(float(os.getenv("WEATHER_CALLBACK_RETRY_DELAY_SECONDS", "1")), 0)
    cache_key = f"beckn:on_search:txn:{transaction_id}"

    for attempt in range(1, retry_count + 1):
        callback_payload = await get_cache(cache_key)
        if not callback_payload:
            if attempt < retry_count:
                await asyncio.sleep(retry_delay_seconds)
            continue

        normalized_data, nack_msg, ack_pending = _normalize_weather_result(callback_payload)
        if nack_msg:
            logger.warning("Weather callback cache returned NACK: %s", nack_msg)
            return None
        if normalized_data is not None and not ack_pending:
            return normalized_data

    return None



@observe(name="tool:weather_forecast", as_type="tool")
async def weather_forecast(latitude: float, longitude: float) -> str:
    """Get Weather forecast for a specific location.

    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
    
    Returns:
        str: The weather forecast for the specific location
    """    
    try:        
        payload = WeatherRequest(latitude=latitude, longitude=longitude).get_payload()
        lf_update_current_observation(
            metadata={"tool": "weather.forecast", "transaction_id": payload.get("context", {}).get("transaction_id")}
        )
        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Weather service configuration error. BAP_ENDPOINT is not set."
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Weather API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT
        )
        if not response.is_success:
            logger.error(
                "Weather API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return "Weather service unavailable. Please try again later."
        logger.info("Weather API response OK")
        data = response.json()
        normalized_data, nack_msg, ack_pending = _normalize_weather_result(data)
        if nack_msg:
            return nack_msg

        if ack_pending:
            transaction_id = payload.get("context", {}).get("transaction_id")

            # First try the callback payload cache written by /api/bap-webhook/on_search.
            normalized_data = await _poll_weather_callback_cache(transaction_id)

            # Short polling helps when the network returns ACK immediately but bundles
            # on_search payloads within subsequent /search reads.
            if normalized_data is None:
                normalized_data = await _poll_weather_async_result(search_url, payload)
            if normalized_data is None:
                return (
                    "Weather request accepted by the network. "
                    "Forecast details will arrive asynchronously via on_search."
                )

        if normalized_data is None:
            return "Weather service returned an unexpected response format."

        data = normalized_data
        weather_response = WeatherResponse.model_validate(data)
        return str(weather_response)
                
    except httpx.TimeoutException:
        logger.error("Weather API request timed out")
        return "Weather request timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Weather API request failed: {e}")
        return f"Weather request failed: {str(e)}"
    except UnexpectedModelBehavior as e:
        logger.warning("Weather request exceeded retry limit")
        return "Weather data is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error getting weather forecast: {e}")
        raise ModelRetry(f"Unexpected error in weather forecast. {str(e)}")