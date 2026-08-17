"""
Offline gazetteer of Kenya's 47 counties.

Used as a lookup ahead of the Photon geocoder so county-level questions resolve
without a network round trip. Coordinates are approximate county-seat /
geographic-centre points — well within the resolution of a weather forecast
grid, but not a substitute for real geocoding of finer-grained places.
"""
import re
from typing import Optional, Tuple

COUNTY_COORDS: dict[str, Tuple[float, float]] = {
    "Mombasa": (-4.0435, 39.6682),
    "Kwale": (-4.1740, 39.4520),
    "Kilifi": (-3.5107, 39.9093),
    "Tana River": (-1.5089, 40.0328),
    "Lamu": (-2.2717, 40.9020),
    "Taita Taveta": (-3.3167, 38.4833),
    "Garissa": (-0.4569, 39.6583),
    "Wajir": (1.7471, 40.0573),
    "Mandera": (3.9366, 41.8670),
    "Marsabit": (2.3284, 37.9899),
    "Isiolo": (0.3556, 37.5820),
    "Meru": (0.0500, 37.6500),
    "Tharaka-Nithi": (-0.2971, 37.7594),
    "Embu": (-0.5310, 37.4500),
    "Kitui": (-1.3667, 38.0167),
    "Makueni": (-1.8000, 37.6200),
    "Machakos": (-1.5177, 37.2634),
    "Nyandarua": (-0.1833, 36.5167),
    "Nyeri": (-0.4169, 36.9514),
    "Kirinyaga": (-0.6591, 37.3826),
    "Murang'a": (-0.7839, 37.0402),
    "Kiambu": (-1.1714, 36.8356),
    "Turkana": (3.1167, 35.6000),
    "West Pokot": (1.6167, 35.3833),
    "Samburu": (1.2167, 36.9500),
    "Trans Nzoia": (1.0167, 34.9500),
    "Uasin Gishu": (0.5167, 35.2833),
    "Elgeyo-Marakwet": (0.8000, 35.5000),
    "Nandi": (0.1833, 35.1167),
    "Baringo": (0.4667, 35.9667),
    "Laikipia": (0.3667, 36.7833),
    "Nakuru": (-0.3031, 36.0800),
    "Narok": (-1.0833, 35.8667),
    "Kajiado": (-1.8500, 36.7833),
    "Kericho": (-0.3667, 35.2833),
    "Bomet": (-0.7833, 35.3417),
    "Kakamega": (0.2827, 34.7519),
    "Vihiga": (0.0500, 34.7167),
    "Bungoma": (0.5667, 34.5667),
    "Busia": (0.4608, 34.1116),
    "Siaya": (0.0607, 34.2881),
    "Kisumu": (-0.0917, 34.7680),
    "Homa Bay": (-0.5273, 34.4571),
    "Migori": (-1.0634, 34.4731),
    "Kisii": (-0.6773, 34.7796),
    "Nyamira": (-0.5633, 34.9358),
    "Nairobi City": (-1.2921, 36.8219),
}

# Alternate names the normalizer alone does not cover.
COUNTY_ALIASES: dict[str, str] = {
    "nairobi": "Nairobi City",
    "homabay": "Homa Bay",
    "keiyo marakwet": "Elgeyo-Marakwet",
    "taveta": "Taita Taveta",
}


def normalize_place(value: str) -> str:
    """Lowercase and fold apostrophes/hyphens so name variants collapse to one key.

    "Murang'a", "Muranga" and "murang a" all normalize to "muranga";
    "Elgeyo-Marakwet" and "Elgeyo Marakwet" both to "elgeyo marakwet".
    """
    folded = (value or "").strip().lower().replace("'", "").replace("’", "")
    folded = folded.replace("-", " ")
    return re.sub(r"\s+", " ", folded).strip()


# normalized key -> canonical county name, longest first so multi-word names
# ("Nairobi City") win over any shorter name nested inside them ("Nairobi").
_COUNTY_BY_KEY: dict[str, str] = {
    **{normalize_place(name): name for name in COUNTY_COORDS},
    **{normalize_place(alias): name for alias, name in COUNTY_ALIASES.items()},
}
_KEYS_BY_LENGTH: list[str] = sorted(_COUNTY_BY_KEY, key=len, reverse=True)


def find_county(text: str) -> Optional[str]:
    """Return the canonical county name mentioned anywhere in `text`, if any.

    Scanning for proper nouns rather than parsing sentence structure means this
    survives word order, phrasing and translation — a Swahili query still spells
    "Embu" as "Embu", where an English-only grammar pattern would miss it.
    """
    normalized = normalize_place(text)
    if not normalized:
        return None

    for key in _KEYS_BY_LENGTH:
        if re.search(rf"\b{re.escape(key)}\b", normalized):
            return _COUNTY_BY_KEY[key]
    return None


def county_coordinates(location: Optional[str]) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) if `location` names a county, else None.

    Falls back to a scan so qualified forms like "Embu County" or
    "Nairobi, Kenya" resolve as readily as the bare name.
    """
    if not location:
        return None

    canonical = _COUNTY_BY_KEY.get(normalize_place(location)) or find_county(location)
    return COUNTY_COORDS.get(canonical) if canonical else None
