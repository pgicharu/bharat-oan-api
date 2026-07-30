"""
Mandi price discovery tool for fetching commodity prices from nearby mandis
using the Vistaar Beckn API.
"""
import os
import uuid
from datetime import date, datetime, timezone
from helpers.utils import get_logger
import httpx
import pytz
from dateutil import parser as dateutil_parser
from app.config import DEFAULT_HTTP_TIMEOUT
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from dotenv import load_dotenv
from langfuse import observe
from pydantic_ai.tools import RunContext
from agents.deps import FarmerContext
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

    def __str__(self) -> str:
        if self.name:
            return self.name
        elif self.code:
            return self.code
        return ""

# -----------------------
# Country, City & Location
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
    display: bool = True

    def __str__(self) -> str:
        items_str = "\n      ".join(str(tag_item) for tag_item in self.list)
        return items_str

# -----------------------
# Stop & Fulfillment
# -----------------------
class Stop(BaseModel):
    location: Location

class Fulfillment(BaseModel):
    id: Optional[str] = None
    stops: Optional[List[Stop]] = None
    tracking: Optional[bool] = None

    def __str__(self) -> str:
        lines = [f"Fulfillment ID: {self.id}"]
        if self.stops:
            for stop in self.stops:
                if stop.location.lat and stop.location.lon:
                    lines.append(f"  Location: {stop.location.lat}, {stop.location.lon}")
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
# Date formatting and matching
# -----------------------
_IST = pytz.timezone("Asia/Kolkata")


def _parse_mandi_date(date_str: Optional[str]) -> Optional[date]:
    """Parse a mandi date string (DD-MM-YYYY or similar) to a calendar date."""
    if not date_str or not date_str.strip():
        return None
    try:
        return dateutil_parser.parse(date_str.strip(), dayfirst=True).date()
    except Exception:
        return None


def _format_price_date_display(price_date: Optional[str]) -> str:
    """Format a price or arrival date for tool output."""
    if not price_date or not price_date.strip():
        return "Latest available"
    try:
        dt = dateutil_parser.parse(price_date.strip(), dayfirst=True)
        return dt.strftime("%A, %d %B %Y")
    except Exception:
        return price_date.strip()


def _item_matches_requested_date(item: "MandiItem", requested_price_date: Optional[str]) -> bool:
    """Return True when no specific date was requested or the item's arrival date matches."""
    if not requested_price_date or not requested_price_date.strip():
        return True
    requested = _parse_mandi_date(requested_price_date)
    if requested is None:
        return True
    arrival = _parse_mandi_date(item._get_tag_value("Arrival Date"))
    return arrival is not None and arrival == requested


def _arrival_date_sort_key(item: "MandiItem") -> datetime:
    """Return a comparable datetime for sorting; items without date sort last (max datetime)."""
    arrival_date = item._get_tag_value("Arrival Date") or ""
    if not arrival_date or not arrival_date.strip():
        return datetime.max.replace(tzinfo=_IST)
    try:
        dt = dateutil_parser.parse(arrival_date.strip(), dayfirst=True)
        return dt.replace(tzinfo=_IST) if dt.tzinfo is None else dt
    except Exception:
        return datetime.max.replace(tzinfo=_IST)


# -----------------------
# MandiItem
# -----------------------
class MandiItem(BaseModel):
    id: str
    descriptor: Descriptor
    matched: bool = False
    category_ids: Optional[List[str]] = None
    fulfillment_ids: Optional[List[str]] = None
    tags: Optional[List[Tag]] = None

    def _get_tag_value(self, code: str) -> Optional[str]:
        """Extract a value from the tags list by descriptor code."""
        if not self.tags:
            return None
        for tag in self.tags:
            for item in tag.list:
                if item.descriptor.code == code:
                    return item.value
        return None

    def __str__(self) -> str:
        commodity = self._get_tag_value("Commodity") or (self.descriptor.name or self.id)
        market = self._get_tag_value("Market") or ""
        district = self._get_tag_value("District") or ""
        state = self._get_tag_value("State") or ""
        modal_price = self._get_tag_value("Modal Price") or ""
        min_price = self._get_tag_value("Min Price") or ""
        max_price = self._get_tag_value("Max Price") or ""
        price_unit = self._get_tag_value("Price Unit") or ""
        arrival_date = self._get_tag_value("Arrival Date") or ""
        variety = self._get_tag_value("Variety") or ""
        grade = self._get_tag_value("Grade") or ""

        location_parts = [p for p in [market, district, state] if p]
        location_str = ", ".join(location_parts)

        lines = []
        lines.append(f"Commodity: {commodity}")
        if location_str:
            lines.append(f"Market: {location_str}")
        if modal_price:
            price_str = f"{price_unit} {modal_price}" if price_unit else modal_price
            if min_price and max_price:
                price_str += f" (Min: {min_price}, Max: {max_price})"
            lines.append(f"Price: {price_str}")
        if arrival_date:
            formatted_date = _format_price_date_display(arrival_date)
            if formatted_date:
                lines.append(f"Arrival Date: {formatted_date}")
        extras = []
        if variety:
            extras.append(f"Variety: {variety}")
        if grade:
            extras.append(f"Grade: {grade}")
        if extras:
            lines.append(" | ".join(extras))

        return "\n".join(lines)

# -----------------------
# Provider
# -----------------------
class Provider(BaseModel):
    id: str
    descriptor: Descriptor
    categories: Optional[List[Category]] = None
    fulfillments: Optional[List[Fulfillment]] = None
    items: Optional[List[MandiItem]] = None

    def __str__(self) -> str:
        lines = []
        if self.items:
            # Newest first (most recent arrival date at top)
            sorted_items = sorted(
                self.items,
                key=_arrival_date_sort_key,
                reverse=True,
            )
            for item in sorted_items:
                lines.append(str(item))
        return "\n---\n".join(lines)

# -----------------------
# Catalog
# -----------------------
class Catalog(BaseModel):
    descriptor: Descriptor
    providers: List[Provider]

    def __str__(self) -> str:
        lines = []
        for provider in self.providers:
            lines.append(str(provider))
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
# Mandi Response
# -----------------------
class MandiResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_mandi_data(self) -> bool:
        """Check if there are any responses with providers that have items."""
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if provider.items and len(provider.items) > 0:
                    return True
        return False

    def _collect_items(self, requested_price_date: Optional[str] = None) -> List["MandiItem"]:
        """Collect items, optionally keeping only those matching the requested date."""
        items: List[MandiItem] = []
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if not provider.items:
                    continue
                for item in provider.items:
                    if _item_matches_requested_date(item, requested_price_date):
                        items.append(item)
        return items

    def format_output(self, requested_price_date: Optional[str] = None) -> str:
        lines = []
        date_label = _format_price_date_display(requested_price_date)
        lines.append(f"**Mandi Price Discovery** [Price Date: {date_label}]")

        if len(self.responses) == 0 or not self._has_mandi_data():
            lines.append("No mandi price data found for the requested location and commodity.")
            return "\n".join(lines)

        matched_items = self._collect_items(requested_price_date)
        if not matched_items:
            lines.append("No mandi price data found for the requested location and commodity.")
            return "\n".join(lines)

        for item in sorted(matched_items, key=_arrival_date_sort_key, reverse=True):
            lines.append(str(item))
        return "\n---\n".join(lines)

    def __str__(self) -> str:
        return self.format_output()

# -----------------------
# Mandi Request
# -----------------------
class MandiRequest(BaseModel):
    """MandiRequest model for mandi price discovery API.

    Args:
        latitude (float): Latitude of the location, example: 26.9124
        longitude (float): Longitude of the location, example: 75.7873
        location_name (str): City or market area name, example: Jaipur
        commodity_name (str): Commodity name, example: Onion
        price_date (str): Optional price date in DD-MM-YYYY; omit only for latest available.
    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    location_name: str = Field(..., description="City or market area name")
    commodity_name: str = Field(..., description="Commodity name in English")
    price_date: Optional[str] = Field(
        default=None,
        description="Optional price date in DD-MM-YYYY; pass for today/yesterday/specific dates; omit only for latest available",
    )

    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the MandiRequest object to a dictionary compatible with Vistaar Beckn API.

        Returns:
            Dict[str, Any]: The dictionary representation of the request payload.
        """
        now = datetime.now(timezone.utc)
        price_date = (
            self.price_date.strip()
            if self.price_date and self.price_date.strip()
            else None
        )

        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "search",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT10M",
                "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                }
            },
            "message": {
                "intent": {
                    "category": {
                        "descriptor": {
                            "code": "price-discovery"
                        }
                    },
                    "item": {
                        "descriptor": {
                            "name": self.commodity_name
                        }
                    },
                    "fulfillment": {
                        "stops": [
                            {
                                "location": {
                                    "descriptor": {
                                        "name": self.location_name
                                    },
                                    "gps": f"{self.latitude},{self.longitude}"
                                }
                            }
                        ],
                        "end": {
                            "location": {
                                "descriptor": {
                                    "name": self.location_name
                                },
                                "gps": f"{self.latitude},{self.longitude}"
                            }
                        }
                    },
                    "tags": [
                        {
                            "code": "date",
                            "value": price_date
                        }
                    ]
                }
            }
        }

@observe(name="tool:get_mandi_prices", as_type="tool")
async def get_mandi_prices(
    ctx: RunContext[FarmerContext],
    latitude: float,
    longitude: float,
    location_name: str,
    commodity_name: str,
    price_date: Optional[str] = None,
) -> str:
    """Get mandi prices for a specific commodity near a location.

    Use this tool to fetch commodity price information from nearby mandis (agricultural markets).
    Use forward_geocode for coordinates and location_name (city or district from the query).
    Use search_commodity to resolve the English commodity name (the name column, e.g. Onion).

    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
        location_name (str): City or market area name (e.g. Jaipur, Pune)
        commodity_name (str): English commodity name from search_commodity (e.g. Onion)
        price_date (str): Optional price date in DD-MM-YYYY; pass for today/yesterday/specific dates.

    Returns:
        str: Formatted mandi price data for the requested commodity and location
    """
    try:
        payload = MandiRequest(
            latitude=latitude,
            longitude=longitude,
            location_name=location_name,
            commodity_name=commodity_name,
            price_date=price_date,
        ).get_payload()
        lf_update_current_observation(
            metadata={
                "tool": "mandi.prices",
                "transaction_id": payload.get("context", {}).get("transaction_id"),
            }
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Mandi service configuration error. BAP_ENDPOINT is not set."
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Mandi API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT
        )
        if response.status_code != 200:
            logger.error(
                "Mandi API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return "Mandi service unavailable. Please try again later."
        logger.info("Mandi API response OK")
        data = response.json()
        mandi_response = MandiResponse.model_validate(data)
        return mandi_response.format_output(requested_price_date=price_date)

    except httpx.TimeoutException:
        logger.error("Mandi API request timed out")
        return "Mandi price request timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Mandi API request failed: {e}")
        return f"Mandi price request failed: {str(e)}"
    except UnexpectedModelBehavior as e:
        logger.warning("Mandi request exceeded retry limit")
        return "Mandi price data is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error getting mandi prices: {e}")
        raise ModelRetry(f"Unexpected error in mandi price request. {str(e)}")
