"""
Qdrant scheme search — matches schemes-index ingested with intfloat/multilingual-e5-large.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import SentenceTransformer

DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
SCHEME_SEARCH_SOURCE = "Government Scheme Information"
_embedder_cache: dict[str, SentenceTransformer] = {}
_qdrant_client_cache: dict[tuple[str, Optional[str]], QdrantClient] = {}

# scheme_code -> scheme_name + aliases.
# scheme_code must match Qdrant payload.
_QDRANT_SCHEME_DEFINITIONS: dict[str, dict[str, Any]] = {
    "mif": {
        "scheme_name": "Micro Irrigation Fund",
        "scheme_aliases": [
            "MIF",
            "micro irrigation fund",
            "Micro Irrigation Fund scheme",
        ],
    },
    "pkvy": {
        "scheme_name": "Paramparagat Krishi Vikas Yojana",
        "scheme_aliases": [
            "PKVY",
            "paramparagat krishi vikas yojana",
            "paramparagat krishi",
            "organic farming scheme",
        ],
    },
    "pm-kmy": {
        "scheme_name": "Pradhan Mantri Kisan Maandhan Yojana",
        "scheme_aliases": [
            "PM-KMY",
            "PMKMY",
            "pm kmy",
            "Kisan Maandhan Yojana",
            "Kisan Mandhan Yojana",
            "kisan mandhan",
            "PMKMY Yojana",
        ],
    },
    "cdp": {
        "scheme_name": "Crop Diversification Programme",
        "scheme_aliases": [
            "CDP",
            "crop diversification programme",
            "crop diversification program",
            "crop diversification",
        ],
    },
    "pulses-mission": {
        "scheme_name": "Mission for Aatmanirbharta in Pulses",
        "scheme_aliases": [
            "pulses mission",
            "aatmanirbharta in pulses",
            "mission for aatmanirbharta in pulses",
            "nfsm pulses",
            "pulses scheme",
            "self reliance in pulses",
        ],
    },
    "cotton-mission": {
        "scheme_name": "Mission for Cotton Productivity",
        "scheme_aliases": [
            "cotton mission",
            "mission for cotton productivity",
            "cotton productivity mission",
            "nfsnm cotton",
            "nfsm cotton",
        ],
    },
    "nmeo": {
        "scheme_name": "National Mission on Edible Oils – Oilseeds",
        "scheme_aliases": [
            "NMEO",
            "NMEO-OS",
            "NMEO OS",
            "edible oils oilseeds",
            "oilseeds mission",
            "national mission on edible oils",
        ],
    },
    "makhana": {
        "scheme_name": "Central Sector Scheme for Development of Makhana",
        "scheme_aliases": [
            "makhana",
            "makana",
            "foxnut",
            "horticulture makhana",
            "makhana scheme",
            "development of makhana",
        ],
    },
}

QDRANT_SCHEME_CODES = frozenset(_QDRANT_SCHEME_DEFINITIONS.keys())

_BUILTIN_SCHEME_LIST: list[dict[str, Any]] = [
    {
        "scheme_code": code,
        "scheme_name": definition["scheme_name"],
        "scheme_aliases": list(definition.get("scheme_aliases", [])),
    }
    for code, definition in _QDRANT_SCHEME_DEFINITIONS.items()
]


def get_builtin_scheme_list() -> list[dict[str, Any]]:
    """Built-in scheme registry used for query → scheme_code resolution."""
    return _BUILTIN_SCHEME_LIST


def format_qdrant_scheme_codes_for_doc() -> str:
    """One-line scheme code list for tool docstrings (like get_scheme_info)."""
    return ", ".join(
        f'"{code}" ({_QDRANT_SCHEME_DEFINITIONS[code]["scheme_name"]})'
        for code in sorted(_QDRANT_SCHEME_DEFINITIONS)
    )

ALIAS_STOPWORDS = {
    "scheme", "schemes", "mission", "national", "programme", "program",
    "yojana", "yojna", "development", "fund", "for", "the", "and", "under",
    "government", "india", "ministry", "guidelines", "operational",
}

_SCHEME_FOCUS_STOPWORDS = ALIAS_STOPWORDS | frozenset({
    "what", "who", "how", "when", "where", "why", "which", "about",
    "farmer", "farmers", "criteria", "details", "information", "available",
    "tell", "explain", "know", "need", "want", "there", "this", "that",
})

FARMER_TERMS = (
    "farmer", "farmers", "farmer's", "farmers'", "farmers' groups",
    "farmer groups", "individual farmer", "all farmers",
    "small and marginal", "land holder", "cultivator",
    "for selection of areas", "for selection of areas/ farmers",
)

HOW_TO_APPLY = "how to apply"
APPLICATION_PROCESS = "application process"
DOCUMENTS_REQUIRED = "documents required"
COST_NORM = "cost norm"
PATTERN_OF_ASSISTANCE = "pattern of assistance"
REGISTRATION_PROCESS = "registration process"

APPLICATION_CHUNK_TERMS = (
    HOW_TO_APPLY,
    APPLICATION_PROCESS,
    DOCUMENTS_REQUIRED,
    "registration",
)
APPLICATION_RERANK_PHRASES = (
    HOW_TO_APPLY,
    APPLICATION_PROCESS,
    REGISTRATION_PROCESS,
    DOCUMENTS_REQUIRED,
    "online portal",
    "apply through",
)
SUPPORT_CHUNK_TERMS = ("support", "subsidy", "assistance", "benefit", COST_NORM)
SUBSIDY_QUERY_TERMS = ("subsidy", "assistance", COST_NORM, "how much")
SUPPORT_RERANK_TERMS = ("benefit", "subsidy", "assistance", "support", "incentive")

_HEADING_FARMER_ELIGIBILITY_MARKERS = (
    "# eligibility", "# eligible", "# criteria",
    "## eligibility", "## eligible", "## criteria",
)
_RE_HEADING_ELIGIBILITY = re.compile(
    r"#{1,6}\s+(?:eligibility|eligible criteria|who can apply)"
)
_RE_HEADING_EXCLUSION = re.compile(
    r"#{1,6}\s+(?:exclusion|scheme exclusion|who cannot apply)"
)
_RE_HEADING_SUPPORT = re.compile(
    r"#{1,6}\s+(?:benefits|support|pattern of assistance)"
)
_RE_NUMBERED_STEPS = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)

INTENT_TERMS = {
    "eligibility": [
        "eligibility", "eligible", "am i eligible", "who can apply",
        "who is eligible", "qualify", "criteria", "qualifying", "for selection of",
    ],
    "application": [
        HOW_TO_APPLY, APPLICATION_PROCESS, "application procedure",
        "application", "apply", "register", "registration", "portal",
        DOCUMENTS_REQUIRED, "how do i apply", "apply for", "procedure", "process",
    ],
    "support": [
        "support", "financial support", "government support", "what support",
        "assistance", "financial assistance", "subsidy", "subsidies",
        "benefit", "benefits", "how much", "fund", "incentive",
        PATTERN_OF_ASSISTANCE, COST_NORM, "help available",
    ],
}

EXCLUSION_TERMS = (
    "excluded", "exclusion", "who cannot apply", "who is excluded",
    "who are excluded", "not eligible", "ineligible", "cannot apply",
)

QUERY_EXPANSIONS = {
    "eligibility": (
        "farmer beneficiary selection criteria who is eligible groups clusters "
        "scheme exclusion who cannot apply ineligible not eligible"
    ),
    "exclusion": "scheme exclusion who cannot apply ineligible not eligible excluded",
    "application": (
        f"{APPLICATION_PROCESS} registration portal {DOCUMENTS_REQUIRED} {HOW_TO_APPLY} "
        "apply online procedure steps"
    ),
    "support": (
        f"scheme benefits subsidy assistance financial support {PATTERN_OF_ASSISTANCE} "
        f"{COST_NORM} incentive fund amount"
    ),
}

INTENT_SECTIONS = {"support": "support", "application": "application"}
LABELED_SECTIONS = frozenset({"eligibility", "exclusion", "support", "application"})

INSTITUTIONAL_TERMS = (
    "regional council", "support agency", "implementing agency",
    "state government", "state/ut", "organisation/agency", "organization/agency",
    "steering committee", "executive committee", "district level executive",
    "state technical unit", "project management team", "state level executive",
    "technical assistant of agencies", "seed growers covered",
)

APP_UI_NOISE_TERMS = (
    "click on", "logout", "offline record", "sync (offline",
    "registered farmer's list", "user manual", "app video guide",
    "language change", "my profile", "triple bar icon",
)


def _alias_in_query(alias: str, query: str) -> bool:
    alias = alias.lower().strip()
    if not alias:
        return False
    if len(alias) <= 6 or "-" in alias:
        return bool(re.search(rf"\b{re.escape(alias)}\b", query))
    return alias in query


def resolve_scheme_code(query: str, scheme_list: list[dict[str, Any]]) -> Optional[str]:
    q = query.lower()
    best_len = 0
    best_code: Optional[str] = None

    for item in scheme_list:
        code = item["scheme_code"]
        candidates = [code, item.get("scheme_name", "")]
        candidates.extend(item.get("scheme_aliases", []))

        for candidate in candidates:
            alias = str(candidate).lower().strip()
            if not alias or alias in ALIAS_STOPWORDS:
                continue
            if not _alias_in_query(alias, q):
                continue
            if len(alias) > best_len:
                best_len = len(alias)
                best_code = code

    return best_code


def _strip_intent_terms(query: str) -> str:
    cleaned = query.lower()
    for terms in INTENT_TERMS.values():
        for term in sorted(terms, key=len, reverse=True):
            cleaned = cleaned.replace(term, " ")
    for term in EXCLUSION_TERMS:
        cleaned = cleaned.replace(term, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def query_names_unindexed_scheme(query: str, scheme_list: list[dict[str, Any]]) -> bool:
    """True when the query names a scheme acronym/title not in the Qdrant registry."""
    if resolve_scheme_code(query, scheme_list):
        return False
    tokens = [
        token
        for token in re.split(r"[\s?,]+", _strip_intent_terms(query))
        if len(token) >= 3 and token not in _SCHEME_FOCUS_STOPWORDS
    ]
    return bool(tokens)


def classify_query_intent(query: str) -> Optional[str]:
    q = query.lower()
    scores = {intent: sum(1 for term in terms if term in q) for intent, terms in INTENT_TERMS.items()}
    best_intent = max(scores, key=scores.get)
    return best_intent if scores[best_intent] > 0 else None


def classify_scheme_section_focus(query: str) -> Optional[str]:
    """Mirrors legacy get_scheme_info eligibility/exclusion routing."""
    q = query.lower()
    has_eligibility = any(term in q for term in INTENT_TERMS["eligibility"])
    has_exclusion = any(term in q for term in EXCLUSION_TERMS)
    if has_exclusion and not has_eligibility:
        return "exclusion_only"
    if has_eligibility:
        return "eligibility_with_exclusion"
    return None


def expand_query_for_search(
    query: str,
    intent: Optional[str] = None,
    section_focus: Optional[str] = None,
) -> str:
    section_focus = section_focus or classify_scheme_section_focus(query)
    if section_focus == "exclusion_only":
        return f"{query} {QUERY_EXPANSIONS['exclusion']}"
    intent = intent or classify_query_intent(query)
    if intent and intent in QUERY_EXPANSIONS:
        return f"{query} {QUERY_EXPANSIONS[intent]}"
    return query


def classify_chunk_section(text: str) -> str:
    text_l = text.lower()
    section_patterns = {
        "exclusion": (
            r"#+\s*scheme exclusion", r"#+\s*exclusion", r"scheme exclusion",
            r"who are excluded", r"who cannot apply", r"not eligible for", r"ineligible",
        ),
        "eligibility": (
            r"#+\s*scheme eligibility", r"#+\s*eligibility", r"scheme eligibility",
            r"who can apply", r"who is eligible", r"eligible criteria", r"eligibility criteria",
        ),
        "application": (
            rf"#+\s*{re.escape(APPLICATION_PROCESS)}",
            rf"#+\s*{re.escape(HOW_TO_APPLY)}",
            re.escape(APPLICATION_PROCESS),
            re.escape(HOW_TO_APPLY),
            re.escape(DOCUMENTS_REQUIRED),
            re.escape(REGISTRATION_PROCESS),
            r"online portal",
        ),
        "support": (
            r"#+\s*benefits", r"#+\s*support", r"scheme benefits",
            re.escape(PATTERN_OF_ASSISTANCE), r"financial support", re.escape(COST_NORM),
            r"subsidy", r"assistance to farmers",
        ),
    }
    scores = {
        section: sum(1 for pattern in patterns if re.search(pattern, text_l))
        for section, patterns in section_patterns.items()
    }
    best_section = max(scores, key=scores.get)
    if scores[best_section] > 0:
        if best_section == "exclusion" and scores["exclusion"] <= scores["eligibility"]:
            return "eligibility" if scores["eligibility"] > 0 else "exclusion"
        return best_section

    if any(term in text_l for term in ("exclusion", "excluded", "cannot apply", "ineligible")):
        return "exclusion"
    if any(term in text_l for term in ("eligibility", "eligible", "who can apply")):
        return "eligibility"
    if any(term in text_l for term in APPLICATION_CHUNK_TERMS):
        return "application"
    if any(term in text_l for term in SUPPORT_CHUNK_TERMS):
        return "support"
    return "other"


def _result_key(item: dict[str, Any]) -> str:
    return str(item.get("chunk_id") or "") or str(id(item))


def _try_add_item(
    item: dict[str, Any],
    selected: list[dict[str, Any]],
    seen: set[str],
) -> bool:
    key = _result_key(item)
    if key in seen:
        return False
    seen.add(key)
    selected.append(item)
    return True


def _fill_section_slots(
    results: list[dict[str, Any]],
    section: str,
    slot_count: int,
    selected: list[dict[str, Any]],
    seen: set[str],
) -> None:
    section_count = 0
    for item in results:
        if item.get("section") != section:
            continue
        if _try_add_item(item, selected, seen):
            section_count += 1
        if section_count >= slot_count:
            break


def _fill_remaining_slots(
    results: list[dict[str, Any]],
    top_k: int,
    selected: list[dict[str, Any]],
    seen: set[str],
) -> None:
    for item in results:
        if len(selected) >= top_k:
            break
        _try_add_item(item, selected, seen)


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in results:
        key = _result_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _take_balanced(
    results: list[dict[str, Any]],
    sections: tuple[str, ...],
    top_k: int,
) -> list[dict[str, Any]]:
    slot_count = max(top_k // len(sections), 1)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for section in sections:
        _fill_section_slots(results, section, slot_count, selected, seen)
    _fill_remaining_slots(results, top_k, selected, seen)
    return selected[:top_k]


def _finalize_results(
    results: list[dict[str, Any]],
    section_focus: Optional[str],
    intent: Optional[str],
    top_k: int,
) -> list[dict[str, Any]]:
    if not results:
        return []

    results = _dedupe_results(results)

    if section_focus == "exclusion_only":
        preferred = [r for r in results if r.get("section") == "exclusion"]
        return (preferred + [r for r in results if r not in preferred])[:top_k]

    if section_focus == "eligibility_with_exclusion":
        return _take_balanced(results, ("eligibility", "exclusion"), top_k)

    section = INTENT_SECTIONS.get(intent or "")
    if section:
        preferred = [r for r in results if r.get("section") == section]
        return (preferred + [r for r in results if r not in preferred])[:top_k]

    return results[:top_k]


def chunk_table_score(text: str) -> float:
    text_l = text.lower()
    score = 0.0
    pipe_density = text.count("|") / max(len(text), 1)
    score += min(pipe_density * 10, 0.55)
    if re.search(r"rs\.?\s*\d", text_l):
        score += 0.12
    if "/ha" in text_l or "per ha" in text_l or "per beneficiary" in text_l:
        score += 0.12
    if COST_NORM in text_l or PATTERN_OF_ASSISTANCE in text_l:
        score += 0.18
    if "annexure" in text_l and pipe_density > 0.015:
        score += 0.08
    return min(score, 1.0)


def chunk_institutional_score(text: str) -> float:
    text_l = text.lower()
    hits = sum(1 for term in INSTITUTIONAL_TERMS if term in text_l)
    return min(hits * 0.09, 0.45)


def chunk_farmer_score(text: str) -> float:
    text_l = text.lower()
    hits = sum(1 for term in FARMER_TERMS if term in text_l)
    if "farmer" in text_l and any(marker in text_l for marker in _HEADING_FARMER_ELIGIBILITY_MARKERS):
        hits += 2
    if "for selection of" in text_l and "farmer" in text_l:
        hits += 3
    if "3.3" in text_l and "criteria" in text_l and "#" in text_l:
        hits += 2
    return min(hits * 0.07, 0.4)


def _eligibility_rerank_adjustments(
    text_l: str, inst_s: float, table_s: float
) -> tuple[float, float]:
    bonus = 0.0
    if _RE_HEADING_ELIGIBILITY.search(text_l):
        bonus += 0.1
    if _RE_HEADING_EXCLUSION.search(text_l):
        bonus += 0.12
    if "regional council" in text_l and "eligible criteria" in text_l:
        inst_s += 0.35
    return bonus, inst_s + (table_s * 0.85)


def _application_rerank_adjustments(
    text: str, text_l: str, inst_s: float, table_s: float
) -> tuple[float, float]:
    bonus = 0.0
    if any(phrase in text_l for phrase in APPLICATION_RERANK_PHRASES):
        bonus += 0.18
    if _RE_NUMBERED_STEPS.search(text):
        bonus += 0.05
    ui_noise = sum(1 for term in APP_UI_NOISE_TERMS if term in text_l)
    penalty = inst_s * 0.6 + table_s * 0.45 + min(ui_noise * 0.12, 0.45)
    return bonus, penalty


def _support_rerank_adjustments(
    text_l: str, inst_s: float, table_s: float, wants_subsidy_tables: bool
) -> tuple[float, float]:
    bonus = 0.0
    if any(term in text_l for term in SUPPORT_RERANK_TERMS):
        bonus += 0.12
    if _RE_HEADING_SUPPORT.search(text_l):
        bonus += 0.1
    penalty = inst_s * 0.25
    if wants_subsidy_tables:
        bonus += table_s * 0.15
    else:
        penalty += table_s * 0.2
    return bonus, penalty


def _default_rerank_adjustments(inst_s: float, table_s: float) -> tuple[float, float]:
    return 0.0, inst_s * 0.25 + table_s * 0.25


def _rerank_bonus_penalty(
    intent: Optional[str],
    text: str,
    text_l: str,
    inst_s: float,
    table_s: float,
    wants_subsidy_tables: bool,
) -> tuple[float, float]:
    if intent == "eligibility":
        return _eligibility_rerank_adjustments(text_l, inst_s, table_s)
    if intent == "application":
        return _application_rerank_adjustments(text, text_l, inst_s, table_s)
    if intent == "support":
        return _support_rerank_adjustments(text_l, inst_s, table_s, wants_subsidy_tables)
    return _default_rerank_adjustments(inst_s, table_s)


def rerank_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not results:
        return results

    intent = classify_query_intent(query)
    q_lower = query.lower()
    intent_terms = [term for terms in INTENT_TERMS.values() for term in terms if term in q_lower]
    wants_subsidy_tables = any(term in q_lower for term in SUBSIDY_QUERY_TERMS)

    def combined_score(item: dict[str, Any]) -> float:
        text = str(item.get("text", ""))
        text_l = text.lower()
        base = float(item["score"])
        keyword_hits = sum(1 for term in intent_terms if term in text_l)
        farmer_s = chunk_farmer_score(text)
        inst_s = chunk_institutional_score(text)
        table_s = chunk_table_score(text)
        bonus = (0.1 * keyword_hits) + farmer_s
        intent_bonus, penalty = _rerank_bonus_penalty(
            intent, text, text_l, inst_s, table_s, wants_subsidy_tables
        )
        return base + bonus + intent_bonus - penalty

    return sorted(results, key=combined_score, reverse=True)


def get_embedder(model_name: str = DEFAULT_EMBEDDING_MODEL) -> SentenceTransformer:
    if model_name not in _embedder_cache:
        logger.info("Loading SentenceTransformer model %s", model_name)
        _embedder_cache[model_name] = SentenceTransformer(model_name)
    return _embedder_cache[model_name]


def warm_scheme_search() -> None:
    """Load E5 embedder at startup so the first search_schemes call does not cold-start."""
    if not os.getenv("QDRANT_URL"):
        logger.info("QDRANT_URL not set; skipping scheme search warm-up")
        return

    started = time.perf_counter()
    try:
        model = get_embedder()
        embed_query("warm-up", model)
        try:
            get_qdrant_client()
        except Exception as exc:
            logger.warning("Qdrant client warm-up skipped: %s", exc)
        logger.info(
            "Scheme search warm-up completed in %.1fs (model=%s)",
            time.perf_counter() - started,
            DEFAULT_EMBEDDING_MODEL,
        )
    except Exception:
        logger.exception(
            "Scheme search warm-up failed; first search_schemes call may be slow"
        )


def embed_query(query: str, model: SentenceTransformer) -> list[float]:
    prefixed = query if query.startswith("query:") else f"query: {query}"
    vector = model.encode([prefixed], normalize_embeddings=False)[0]
    return vector.tolist()


def get_qdrant_client(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> QdrantClient:
    qdrant_url = url or os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_key = api_key if api_key is not None else os.getenv("QDRANT_API_KEY")
    cache_key = (qdrant_url, qdrant_key)
    if cache_key in _qdrant_client_cache:
        return _qdrant_client_cache[cache_key]

    is_local = any(host in qdrant_url for host in ("localhost", "127.0.0.1"))
    if not qdrant_key and not is_local:
        raise ValueError("QDRANT_API_KEY is required for remote Qdrant")
    if qdrant_key:
        client = QdrantClient(url=qdrant_url, api_key=qdrant_key, check_compatibility=False)
    else:
        client = QdrantClient(url=qdrant_url, check_compatibility=False)
    _qdrant_client_cache[cache_key] = client
    return client


def _hit_to_result(hit: Any) -> dict[str, Any]:
    payload = hit.payload or {}
    text = str(payload.get("text") or "")
    return {
        "score": hit.score,
        "scheme_code": payload.get("scheme_code"),
        "scheme_name": payload.get("scheme_name"),
        "text": text,
        "doc_id": payload.get("doc_id"),
        "chunk_id": payload.get("chunk_id"),
        "section": classify_chunk_section(text),
    }


def _hits_to_results(hits: list) -> list[dict[str, Any]]:
    return [_hit_to_result(hit) for hit in hits]


def _run_scheme_search(
    client: QdrantClient,
    collection_name: str,
    query_vector: list[float],
    fetch_k: int,
    code: Optional[str] = None,
    vector: Optional[list[float]] = None,
) -> list:
    query_filter = Filter(must=[FieldCondition(key="type", match=MatchValue(value="scheme"))])
    if code:
        query_filter.must.append(
            FieldCondition(key="scheme_code", match=MatchValue(value=code))
        )
    return client.query_points(
        collection_name=collection_name,
        query=vector if vector is not None else query_vector,
        query_filter=query_filter,
        limit=fetch_k,
        with_payload=True,
    ).points


def _supplemental_search_config(
    section_focus: Optional[str],
    intent: Optional[str],
) -> Optional[tuple[str, str]]:
    if section_focus == "eligibility_with_exclusion":
        return ("exclusion", "exclusion")
    if intent in INTENT_SECTIONS:
        return (INTENT_SECTIONS[intent], intent)
    return None


def _merge_supplemental_results(
    query: str,
    results: list[dict[str, Any]],
    supplemental: tuple[str, str],
    scheme_code: Optional[str],
    client: QdrantClient,
    collection_name: str,
    fetch_k: int,
    model: SentenceTransformer,
) -> list[dict[str, Any]]:
    needed_section, expansion_key = supplemental
    if any(r.get("section") == needed_section for r in results):
        return results
    extra_vector = embed_query(f"{query} {QUERY_EXPANSIONS[expansion_key]}", model)
    extra_hits = _run_scheme_search(
        client, collection_name, extra_vector, fetch_k, scheme_code, extra_vector
    )
    return _dedupe_results(results + _hits_to_results(extra_hits))


def _filter_results_by_scheme(
    results: list[dict[str, Any]],
    scheme_code: Optional[str],
) -> list[dict[str, Any]]:
    if scheme_code:
        return [r for r in results if r.get("scheme_code") == scheme_code]
    return [r for r in results if r.get("scheme_code") in QDRANT_SCHEME_CODES]


def search_schemes(
    query: str,
    collection_name: str,
    scheme_code: Optional[str] = None,
    top_k: int = 5,
    model: Optional[SentenceTransformer] = None,
    client: Optional[QdrantClient] = None,
    scheme_list: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """
    Dense vector search on schemes-index.
    Payload fields: type, scheme_code, scheme_name, scheme_aliases, doc_id, chunk_id, chunk_index, text
    """
    client = client or get_qdrant_client()
    model = model or get_embedder()
    scheme_list = scheme_list if scheme_list is not None else _BUILTIN_SCHEME_LIST

    if scheme_code is None:
        scheme_code = resolve_scheme_code(query, scheme_list)

    if scheme_code is None and query_names_unindexed_scheme(query, scheme_list):
        logger.info("Query names a scheme outside the Qdrant index: %r", query)
        return []

    intent = classify_query_intent(query)
    section_focus = classify_scheme_section_focus(query)
    query_vector = embed_query(expand_query_for_search(query, intent, section_focus), model)
    fetch_k = max(top_k * 8, 40)

    hits = _run_scheme_search(client, collection_name, query_vector, fetch_k, scheme_code)
    results = _hits_to_results(hits)

    supplemental = _supplemental_search_config(section_focus, intent)
    if supplemental:
        results = _merge_supplemental_results(
            query, results, supplemental, scheme_code,
            client, collection_name, fetch_k, model,
        )

    results = _filter_results_by_scheme(results, scheme_code)
    results = rerank_results(query, results)
    return _finalize_results(results, section_focus, intent, top_k)


def _process_chunk_text(text: str) -> str:
    cleaned = re.sub(r"\n{2,}", "\n\n", text)
    cleaned = re.sub(r"\t+", "\t", cleaned)
    from agents.tools.terms import normalize_text_with_glossary

    return normalize_text_with_glossary(cleaned)


def format_scheme_unavailable(_query: str) -> str:
    return "**Scheme not available right now.**\n\n"


def _empty_search_message(query: str, scheme_list: list[dict[str, Any]]) -> str:
    if query_names_unindexed_scheme(query, scheme_list):
        return format_scheme_unavailable(query)
    if resolve_scheme_code(query, scheme_list):
        return "**Could not find this information right now.**\n\n"
    return format_scheme_unavailable(query)


def _format_result_block(item: dict[str, Any]) -> str:
    scheme_name = str(item.get("scheme_name") or "")
    scheme_code = str(item.get("scheme_code") or "")
    text_value = str(item.get("text") or "")
    section = str(item.get("section") or classify_chunk_section(text_value))
    text = _process_chunk_text(text_value)
    score = float(item.get("score") or 0.0)
    section_label = section.title() if section in LABELED_SECTIONS else "General"
    return (
        f"**{scheme_name}** ({scheme_code}, section={section_label}, score={score:.4f})\n"
        f"```\n{text}\n```"
    )


def format_search_results(
    results: list[dict[str, Any]],
    query: str,
    scheme_list: Optional[list[dict[str, Any]]] = None,
) -> str:
    if not results:
        active_scheme_list = scheme_list if scheme_list is not None else _BUILTIN_SCHEME_LIST
        return _empty_search_message(query, active_scheme_list)

    blocks = [_format_result_block(item) for item in results]
    return (
        f"> Scheme Search Results for `{query}`\n\n"
        f"**Source: {SCHEME_SEARCH_SOURCE}**\n\n"
        + "\n\n----\n\n".join(blocks)
    )
