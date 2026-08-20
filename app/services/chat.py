import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, AsyncGenerator, Optional, Tuple
from urllib.parse import quote

from fastapi import BackgroundTasks
import httpx
from langfuse import get_client, observe, propagate_attributes
from pydantic_ai import AgentRunResultEvent, FinalResultEvent, PartDeltaEvent, TextPartDelta
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from agents.agrinet import agrinet_agent
from agents.deps import FarmerContext
from agents.tools.maps import forward_geocode
from agents.tools.kenya_counties import county_coordinates, find_county
from agents.tools.weather import weather_forecast
from agents.tools.farmer_registry import fetch_farmer_profile_context
from agents.models import (
    LANGFUSE_MODERATION_MODEL_NAME,
    LLM_PROVIDER,
    AgrinetRoute,
    get_agrinet_route_model,
    get_agrinet_route_model_name,
)
from agents.moderation import QueryModerationResult, moderation_agent
from app.config import settings
from app.services.agrinet_routing import (
    AgrinetRouteDecision,
    get_alternate_agrinet_route,
    refresh_session_agrinet_route_ttl,
    resolve_agrinet_route,
    set_session_agrinet_route,
)
from app.services.chat_turn_map import write_chat_turn_map
from app.tasks.telemetry import send_telemetry
from app.utils import (
    filter_thinking_from_history,
    format_message_pairs,
    trim_history,
    update_message_history,
)
from helpers.langfuse_trace_schema import (
    AGENT_MODERATION,
    AGENT_VISTAAR,
    chat_trace_metadata_strings,
)
from helpers.langfuse_tracing import lf_update_current_observation, lf_update_current_span
from helpers.telemetry import (
    TelemetryRequest,
    create_chat_answer_event,
    create_chat_error_event,
    create_chat_question_event,
    create_frontend_compatible_item_batch,
)
from helpers.utils import get_crop_season, get_logger, get_prompt, get_today_date_str

logger = get_logger(__name__)

CHAT_TRACE_NAME = (
    os.getenv("LANGFUSE_TRACE_NAME")
    or os.getenv("LANGFUSE_TRACE_ROOT_NAME")
    or "bharat-vistaar-chat"
)
CHAT_CHAIN_SPAN_NAME = "chain.chat"


@dataclass
class _AgrinetCompletedRun:
    result: Any
    output_text: str


class _BedrockRunResult:
    """Small adapter that mimics the pydantic-ai run result API we consume."""

    def __init__(self, output_text: str, messages: list, usage: RequestUsage):
        self.output = output_text
        self._messages = messages
        self._usage = usage

    def new_messages(self):
        return self._messages

    def usage(self):
        return self._usage


@dataclass
class _AgrinetStreamState:
    final_result_found: bool = False
    inside_think_block: bool = False
    raw_chunks: list[str] = field(default_factory=list)


def _is_bedrock_mode() -> bool:
    return (LLM_PROVIDER or "").strip().lower() == "bedrock"


def _bedrock_auth_header_value(token: str) -> str:
    auth_scheme = os.getenv("BEDROCK_AUTH_SCHEME")
    auth_scheme = "Bearer" if auth_scheme is None else auth_scheme.strip()
    if auth_scheme.lower() == "none":
        auth_scheme = ""
    return f"{auth_scheme} {token}" if auth_scheme else token


def _get_bedrock_request_headers() -> dict[str, str]:
    token = (os.getenv("BEDROCK_API_KEY") or os.getenv("BEDROCK_TOKEN") or "").strip()
    if not token:
        raise ValueError("BEDROCK_API_KEY or BEDROCK_TOKEN environment variable is required")

    auth_header = (os.getenv("BEDROCK_AUTH_HEADER") or "Authorization").strip()
    return {
        "Content-Type": "application/json",
        auth_header: _bedrock_auth_header_value(token),
    }


def _get_bedrock_base_url() -> str:
    base_url = (os.getenv("BEDROCK_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("BEDROCK_BASE_URL environment variable is required")
    return base_url


def _extract_bedrock_text(response_payload: dict[str, Any]) -> str:
    content = ((response_payload.get("output") or {}).get("message") or {}).get("content") or []
    texts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "".join(texts).strip()


def _extract_bedrock_usage(response_payload: dict[str, Any]) -> RequestUsage:
    usage = response_payload.get("usage") or {}
    return RequestUsage(
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0),
    )


async def _call_bedrock_converse(
    *,
    model_name: str,
    user_message: str,
    system_prompt: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
) -> tuple[str, RequestUsage]:
    url = f"{_get_bedrock_base_url()}/model/{quote(model_name, safe='')}/converse"
    payload = {
        "system": [{"text": system_prompt}] if system_prompt else [],
        "messages": [{"role": "user", "content": [{"text": user_message}]}],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
            "topP": top_p,
        },
    }

    timeout = httpx.Timeout(timeout_seconds, read=timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=_get_bedrock_request_headers(), json=payload)
        response.raise_for_status()
        data = response.json()

    output_text = _extract_bedrock_text(data)
    if not output_text:
        raise ValueError("Bedrock response did not contain output.message.content[].text")
    return output_text, _extract_bedrock_usage(data)


def _normalize_moderation_category(category: str | None) -> str:
    allowed = {
        "valid_agricultural",
        "invalid_non_agricultural",
        "invalid_external_reference",
        "invalid_compound_mixed",
        "invalid_language",
        "unsafe_illegal",
        "political_controversial",
        "role_obfuscation",
    }
    if not category:
        return "valid_agricultural"

    normalized = category.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in allowed else "valid_agricultural"


def _parse_moderation_json(text: str) -> QueryModerationResult:
    # Extract the first JSON object if the model wrapped it in prose.
    match = re.search(r"\{[\s\S]*\}", text)
    payload_text = match.group(0) if match else text
    payload = json.loads(payload_text)

    category = _normalize_moderation_category(str(payload.get("category", "")))
    action = str(payload.get("action") or "Proceed with agricultural assistance.").strip()
    return QueryModerationResult(category=category, action=action)


async def _run_bedrock_moderation(user_message: str) -> QueryModerationResult:
    prompt = get_prompt("moderation_system")
    schema_instruction = (
        "Return ONLY valid JSON with keys category and action. "
        "Allowed category values: valid_agricultural, invalid_non_agricultural, "
        "invalid_external_reference, invalid_compound_mixed, invalid_language, "
        "unsafe_illegal, political_controversial, role_obfuscation."
    )

    output_text, _ = await _call_bedrock_converse(
        model_name=LANGFUSE_MODERATION_MODEL_NAME,
        user_message=user_message,
        system_prompt=f"{prompt}\n\n{schema_instruction}",
        temperature=0.0,
        top_p=1.0,
        max_tokens=512,
        timeout_seconds=float(os.getenv("LLM_MODERATION_TIMEOUT_SECONDS", "20")),
    )

    try:
        return _parse_moderation_json(output_text)
    except Exception:
        logger.warning("Bedrock moderation output was not valid JSON; falling back to safe default")
        return QueryModerationResult(
            category="valid_agricultural",
            action="Proceed with agricultural assistance.",
        )


def _build_agrinet_system_prompt(deps: FarmerContext) -> str:
    return get_prompt(
        "agrinet_en",
        context={
            "today_date": get_today_date_str(),
            "crop_season": get_crop_season(),
        },
    )


def _agrinet_route_metadata(
    decision: AgrinetRouteDecision,
    *,
    fallback_used: bool,
    fallback_from: AgrinetRoute | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "agrinet_route": decision.route,
        "agrinet_model_name": decision.model_name,
        "route_source": decision.source,
        "fallback_used": fallback_used,
    }
    if fallback_from:
        metadata["fallback_from"] = fallback_from
    return metadata


def _strip_streaming_thinking_chunk(chunk: str, state: _AgrinetStreamState) -> str:
    if not chunk:
        return ""

    visible = chunk
    if state.inside_think_block:
        end_idx = visible.find("</think>")
        if end_idx < 0:
            return ""
        state.inside_think_block = False
        visible = visible[end_idx + len("</think>") :]

    if "<think>" in visible:
        end_idx = visible.find("</think>")
        if end_idx >= 0:
            visible = re.sub(r"<think>[\s\S]*?</think>", "", visible)
        else:
            state.inside_think_block = True
            visible = visible[: visible.find("<think>")]

    return visible


def _sanitize_streamed_output(raw_output: str) -> str:
    cleaned_output = re.sub(r"<think>[\s\S]*?</think>", "", raw_output)
    cleaned_output = re.sub(r"<think>[\s\S]*$", "", cleaned_output)
    return cleaned_output.strip()


def _looks_like_weather_query(query: str) -> bool:
    text = (query or "").strip().lower()
    if not text:
        return False
    keywords = (
        "weather",
        "forecast",
        "temperature",
        "rain",
        "humidity",
        "wind",
        # Swahili. Deliberately limited to the unambiguous weather phrase: bare
        # "mvua"/"joto" are everyday farming vocabulary and would divert ordinary
        # agronomy questions into the weather path.
        "hali ya hewa",
    )
    return any(keyword in text for keyword in keywords)


def _extract_lat_lon_from_text(text: str) -> tuple[float, float] | None:
    if not text:
        return None

    named_match = re.search(
        r"latitude\s*[:=]?\s*(-?\d+(?:\.\d+)?)\D+longitude\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if named_match:
        return float(named_match.group(1)), float(named_match.group(2))

    bare_match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text)
    if not bare_match:
        return None

    lat = float(bare_match.group(1))
    lon = float(bare_match.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


# Possessive/generic references to "wherever the farmer already is" — never
# real place names. A bare regex capture of "weather in my county" would
# otherwise hand "my county" to county_coordinates/forward_geocode as if it
# were a place, which reliably fails (no county is literally named "my
# county") instead of falling through to the farmer's actual registered
# county below.
_GENERIC_LOCATION_PHRASES = {
    "my county", "my district", "my area", "my location", "my place",
    "my region", "my farm", "our county", "our district", "our area",
    "here", "near me", "this area", "the area", "this county", "this district",
}


def _is_generic_location_phrase(text: str) -> bool:
    return (text or "").strip().lower() in _GENERIC_LOCATION_PHRASES


def _extract_location_from_weather_query(query: str) -> str | None:
    text = (query or "").strip()
    if not text:
        return None

    county = find_county(text)
    if county:
        return county

    patterns = [
        r"(?:weather|forecast)\s+(?:\bat\b|\bin\b|\bfor\b)\s+([A-Za-z\s,.-]{2,})",
        r"(?:\bat\b|\bin\b|\bfor\b)\s+([A-Za-z\s,.-]{2,})\s+(?:weather|forecast)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        location = re.sub(r"\s+", " ", match.group(1)).strip(" .?,-")
        location = re.sub(
            r"\b(today|tomorrow|tonight|now|currently|right now|this week|next week)\b$",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip(" .?,-")
        if not location or _is_generic_location_phrase(location):
            return None
        return location

    return None


async def _resolve_weather_coordinates(
    raw_query: str,
    farmer_coordinates: Optional[Tuple[float, float]] = None,
) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """Deterministically resolve (lat, lon) for a weather-shaped query.

    Resolution order: explicit lat/lon already in the query text -> a named
    place in the query (offline Kenya county table, else a live
    `forward_geocode` call) -> the farmer's registered county, already
    resolved via that same offline table (see
    `agents.tools.farmer_registry.fetch_farmer_profile_context`). This runs
    entirely in code rather than relying on the model to notice a phrase like
    "my county" isn't a real place and chain lookup -> geocode -> weather
    itself — see that module's docstring for why.

    Returns:
        `(coordinates, geocode_error)`. `geocode_error` is set only when an
        explicit place was named but a live `forward_geocode` lookup for it
        failed — the caller can surface that failure directly instead of
        silently falling through to a full model call that might hallucinate
        a location. Both are None when the query has no place, no browser
        coordinates, and no farmer profile to fall back on.
    """
    coords = _extract_lat_lon_from_text(raw_query)
    if coords:
        return coords, None

    geocode_error: Optional[str] = None
    location = _extract_location_from_weather_query(raw_query)
    if location:
        # The county table is authoritative and offline, so prefer it over a
        # geocoder round trip that may be slow, unreachable, or ambiguous.
        coords = county_coordinates(location)
        if coords is None:
            geocode_result = await forward_geocode(location)
            coords = _extract_lat_lon_from_text(geocode_result)
            if coords is None:
                geocode_error = geocode_result

    if coords is None and geocode_error is None:
        coords = farmer_coordinates

    return coords, geocode_error


async def _try_weather_tool_response(
    raw_query: str,
    farmer_coordinates: Optional[Tuple[float, float]] = None,
) -> str | None:
    if not _looks_like_weather_query(raw_query):
        return None

    coords, geocode_error = await _resolve_weather_coordinates(raw_query, farmer_coordinates)

    if coords is None:
        # Keep weather responses tool-driven: surface a location/tool error
        # instead of falling through to a full model call that might
        # hallucinate a location. None (no error either) means we simply had
        # nothing to go on — let the caller ask the farmer normally.
        return geocode_error

    lat, lon = coords
    logger.info("Weather query routed through tool path with coordinates: %s, %s", lat, lon)
    return await weather_forecast(latitude=lat, longitude=lon)


async def _weather_location_note(
    query: str, farmer_coordinates: Optional[Tuple[float, float]]
) -> Optional[str]:
    """Build a short, high-salience note handing the agent already-resolved
    coordinates for a weather-shaped query, so it only has to copy two
    numbers into `weather_forecast` instead of noticing a possessive phrase
    like "my county" isn't a place name and chaining farmer-profile lookup ->
    geocode -> weather itself — see `_resolve_weather_coordinates` for why
    that chain wasn't reliable. Returns None for non-weather queries or when
    nothing resolves (explicit place, browser coordinates, and farmer
    profile all absent) — the agent falls back to asking the farmer, as
    before.
    """
    if not _looks_like_weather_query(query):
        return None
    coords, _geocode_error = await _resolve_weather_coordinates(query, farmer_coordinates)
    if coords is None:
        return None
    lat, lon = coords
    return (
        f"**Resolved location for this weather query:** latitude={lat}, longitude={lon}. "
        "Call weather_forecast with these exact numbers for this turn — do not call "
        "forward_geocode again."
    )


async def _record_chat_turn(
    trace_id: Optional[str],
    telemetry_qid: str,
    session_id: str,
    decision: AgrinetRouteDecision,
    channel: str,
) -> None:
    """Persist the qid -> Langfuse trace mapping so user feedback can be scored later."""
    if not trace_id:
        logger.warning(
            "No active Langfuse trace id for qid %s; feedback score will be skipped",
            telemetry_qid,
        )
        return
    await write_chat_turn_map(
        telemetry_qid,
        trace_id=trace_id,
        session_id=session_id,
        model_name=decision.model_name,
        agrinet_route=decision.route,
        channel=channel,
    )


@observe(name=CHAT_CHAIN_SPAN_NAME, as_type="chain")
async def stream_chat_messages(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list,
    background_tasks: BackgroundTasks,
    channel: str = "BharatVistaar",
    qid: str = "",
    current_user: Optional[dict] = None,
    farmer_token: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    telemetry_user = current_user or {"channel": channel}
    telemetry_qid = qid or f"chat_{session_id}"
    question_event = create_chat_question_event(
        current_user=telemetry_user,
        qid=telemetry_qid,
        question_text=query,
        session_id=session_id,
    )
    background_tasks.add_task(
        send_telemetry,
        TelemetryRequest(events=create_frontend_compatible_item_batch(question_event)).model_dump(),
    )

    route_decision = await resolve_agrinet_route(session_id, has_history=bool(history))
    logger.info(
        "Resolved agrinet route %s (%s) for session %s via %s",
        route_decision.route,
        route_decision.model_name,
        session_id,
        route_decision.source,
    )
    route_metadata = _agrinet_route_metadata(route_decision, fallback_used=False)

    lf_env = settings.langfuse_tracing_environment
    trace_meta = chat_trace_metadata_strings(
        source_lang=source_lang,
        target_lang=target_lang,
        environment=lf_env,
        channel=channel,
        query=query,
        qid=telemetry_qid,
    )
    trace_meta.update(
        {
            "agrinet_route": route_decision.route,
            "agrinet_model_name": route_decision.model_name,
            "route_source": route_decision.source,
            "fallback_used": "false",
        }
    )
    trace_tags = list(
        dict.fromkeys(
            [
                f"env:{lf_env}",
                f"channel:{channel}",
                f"model:{route_decision.model_name}",
                f"moderation:{LANGFUSE_MODERATION_MODEL_NAME}",
            ]
        )
    )

    with propagate_attributes(
        user_id=user_id,
        session_id=session_id,
        metadata=trace_meta,
        tags=trace_tags,
        trace_name=CHAT_TRACE_NAME,
    ):
        try:
            lf_update_current_span(input=query, metadata=route_metadata)

            trace_id = get_client().get_current_trace_id()
            await _record_chat_turn(trace_id, telemetry_qid, session_id, route_decision, channel)

            deps = FarmerContext(
                query=query,
                lang_code=target_lang,
                session_id=session_id,
                farmer_token=farmer_token,
            )

            # Fetch the farmer profile deterministically, concurrently with
            # moderation (independent of it) — this used to be an
            # agent-callable tool the model decided whether to invoke, but it
            # reliably skipped calling it even with explicit instructions to.
            # See agents.tools.farmer_registry's module docstring.
            farmer_profile_task = (
                asyncio.create_task(fetch_farmer_profile_context(farmer_token))
                if farmer_token
                else None
            )

            message_pairs = "\n\n".join(format_message_pairs(history, 3))
            logger.info("Message pairs: %s", message_pairs)
            last_response = (
                f"**Conversation**\n\n{message_pairs}\n\n---\n\n" if message_pairs else ""
            )

            user_message = f"{last_response}{deps.get_user_message()}"

            moderation_data = await _run_moderation(user_message, session_id)
            logger.info("Moderation data: %s", moderation_data)
            deps.update_moderation_str(str(moderation_data))

            if farmer_profile_task is not None:
                farmer_profile_text, farmer_coordinates = await farmer_profile_task
                deps.update_farmer_profile(farmer_profile_text, farmer_coordinates)

            weather_note = await _weather_location_note(query, deps.farmer_coordinates)
            if weather_note:
                last_response = f"{last_response}{weather_note}\n\n"

            user_message = f"{last_response}{deps.get_user_message()}"

            trimmed_history = trim_history(history, max_tokens=64_000)
            logger.info("Trimmed history length: %s messages", len(trimmed_history))
            trimmed_history = filter_thinking_from_history(trimmed_history)

            chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()
            with propagate_attributes(tags=[moderation_data.category]):
                agrinet_task = asyncio.create_task(
                    _run_agrinet_with_failover_streaming(
                        user_message=user_message,
                        trimmed_history=trimmed_history,
                        deps=deps,
                        session_id=session_id,
                        user_id=user_id,
                        moderation_category=moderation_data.category,
                        initial_decision=route_decision,
                        chunk_queue=chunk_queue,
                    )
                )
                while True:
                    chunk = await chunk_queue.get()
                    if chunk is None:
                        break
                    yield chunk

                completed_run, final_route_decision, fallback_used = await agrinet_task

            final_route_metadata = _agrinet_route_metadata(
                final_route_decision,
                fallback_used=fallback_used,
                fallback_from=route_decision.route if fallback_used else None,
            )

            if fallback_used:
                await _record_chat_turn(
                    trace_id, telemetry_qid, session_id, final_route_decision, channel
                )

            result = completed_run.result
            output_text = completed_run.output_text
            new_messages = result.new_messages()
            logger.info(
                "Agent run complete for session %s via route %s (%s)",
                session_id,
                final_route_decision.route,
                final_route_decision.model_name,
            )

            lf_update_current_span(output=output_text, metadata=final_route_metadata)

            answer_event = create_chat_answer_event(
                current_user=telemetry_user,
                qid=telemetry_qid,
                question_text=query,
                answer_text=output_text,
                session_id=session_id,
            )
            background_tasks.add_task(
                send_telemetry,
                TelemetryRequest(events=create_frontend_compatible_item_batch(answer_event)).model_dump(),
            )

            clean_new_messages = filter_thinking_from_history(list(new_messages or []))
            clean_new_messages = _replace_last_text_output(clean_new_messages, output_text)
            messages = [*history, *clean_new_messages]
            logger.info(
                "Updating message history for session %s with %s messages",
                session_id,
                len(messages),
            )
            await update_message_history(session_id, messages)
            await refresh_session_agrinet_route_ttl(session_id)

            get_client().flush()
        except Exception as exc:
            error_event = create_chat_error_event(
                current_user=telemetry_user,
                qid=telemetry_qid,
                session_id=session_id,
                error_text=f"{type(exc).__name__}: {exc}",
                question_text=query,
            )
            background_tasks.add_task(
                send_telemetry,
                TelemetryRequest(events=create_frontend_compatible_item_batch(error_event)).model_dump(),
            )
            if "agrinet_task" in locals() and not agrinet_task.done():
                agrinet_task.cancel()
            raise


@observe(name=AGENT_MODERATION, as_type="agent")
async def _run_moderation(user_message: str, session_id: str):
    """Run moderation agent and trace it in Langfuse."""
    lf_update_current_observation(
        input=user_message,
        model=LANGFUSE_MODERATION_MODEL_NAME,
        metadata={"session_id": session_id},
    )

    if _is_bedrock_mode():
        run_output = await _run_bedrock_moderation(user_message)
        lf_update_current_observation(
            output=str(run_output),
            model=LANGFUSE_MODERATION_MODEL_NAME,
            metadata={"session_id": session_id, "provider": "bedrock"},
        )
        return run_output

    run = await moderation_agent.run(user_message)

    usage_data = run.usage()
    lf_update_current_observation(
        output=str(run.output),
        model=LANGFUSE_MODERATION_MODEL_NAME,
        input_tokens=usage_data.input_tokens or 0,
        output_tokens=usage_data.output_tokens or 0,
        metadata={"session_id": session_id},
    )
    return run.output


def _replace_last_text_output(messages: list, output_text: str) -> list:
    """Store the same streamed response that the farmer received."""
    if not messages or not output_text:
        return messages

    copied = deepcopy(messages)
    for message in reversed(copied):
        for part in reversed(getattr(message, "parts", []) or []):
            if getattr(part, "part_kind", "") == "text" and hasattr(part, "content"):
                part.content = output_text
                return copied
    return copied


async def _run_agrinet_with_failover(
    user_message: str,
    trimmed_history: list,
    deps: FarmerContext,
    session_id: str,
    user_id: str,
    moderation_category: str,
    initial_decision: AgrinetRouteDecision,
):
    try:
        result = await _run_agrinet_once(
            user_message=user_message,
            trimmed_history=trimmed_history,
            deps=deps,
            session_id=session_id,
            user_id=user_id,
            moderation_category=moderation_category,
            decision=initial_decision,
            fallback_used=False,
        )
        return result, initial_decision, False
    except Exception as primary_exc:
        if not settings.agrinet_routing_enabled:
            raise

        fallback_route = get_alternate_agrinet_route(initial_decision.route)
        fallback_decision = AgrinetRouteDecision(
            route=fallback_route,
            model_name=get_agrinet_route_model_name(fallback_route),
            source="failover",
        )
        logger.warning(
            "Agrinet route %s failed for session %s; retrying on %s (%s): %s",
            initial_decision.route,
            session_id,
            fallback_decision.route,
            fallback_decision.model_name,
            primary_exc,
        )
        result = await _run_agrinet_once(
            user_message=user_message,
            trimmed_history=trimmed_history,
            deps=deps,
            session_id=session_id,
            user_id=user_id,
            moderation_category=moderation_category,
            decision=fallback_decision,
            fallback_used=True,
            fallback_from=initial_decision.route,
        )
        await set_session_agrinet_route(session_id, fallback_decision.route)
        return result, fallback_decision, True


async def _run_agrinet_with_failover_streaming(
    user_message: str,
    trimmed_history: list,
    deps: FarmerContext,
    session_id: str,
    user_id: str,
    moderation_category: str,
    initial_decision: AgrinetRouteDecision,
    chunk_queue: asyncio.Queue[str | None],
):
    try:
        try:
            completed_run = await _run_agrinet_once_streaming(
                user_message=user_message,
                trimmed_history=trimmed_history,
                deps=deps,
                session_id=session_id,
                user_id=user_id,
                moderation_category=moderation_category,
                decision=initial_decision,
                fallback_used=False,
                chunk_queue=chunk_queue,
            )
            return completed_run, initial_decision, False
        except Exception as primary_exc:
            if not settings.agrinet_routing_enabled:
                raise

            fallback_route = get_alternate_agrinet_route(initial_decision.route)
            fallback_decision = AgrinetRouteDecision(
                route=fallback_route,
                model_name=get_agrinet_route_model_name(fallback_route),
                source="failover",
            )
            logger.warning(
                "Agrinet route %s failed for session %s; retrying on %s (%s): %s",
                initial_decision.route,
                session_id,
                fallback_decision.route,
                fallback_decision.model_name,
                primary_exc,
            )
            completed_run = await _run_agrinet_once_streaming(
                user_message=user_message,
                trimmed_history=trimmed_history,
                deps=deps,
                session_id=session_id,
                user_id=user_id,
                moderation_category=moderation_category,
                decision=fallback_decision,
                fallback_used=True,
                fallback_from=initial_decision.route,
                chunk_queue=chunk_queue,
            )
            await set_session_agrinet_route(session_id, fallback_decision.route)
            return completed_run, fallback_decision, True
    finally:
        await chunk_queue.put(None)


@observe(name=AGENT_VISTAAR, as_type="agent")
async def _run_agrinet_once_streaming(
    user_message: str,
    trimmed_history: list,
    deps: FarmerContext,
    session_id: str,
    user_id: str,
    moderation_category: str,
    decision: AgrinetRouteDecision,
    fallback_used: bool,
    chunk_queue: asyncio.Queue[str | None] | None = None,
    fallback_from: AgrinetRoute | None = None,
) -> _AgrinetCompletedRun:
    route_metadata = _agrinet_route_metadata(
        decision,
        fallback_used=fallback_used,
        fallback_from=fallback_from,
    )
    observation_metadata = {
        "session_id": session_id,
        "user_id": user_id,
        "moderation_category": moderation_category,
        **route_metadata,
    }

    lf_update_current_observation(
        input=user_message,
        model=decision.model_name,
        metadata=observation_metadata,
    )

    if _is_bedrock_mode():
        weather_output = await _try_weather_tool_response(deps.query, deps.farmer_coordinates)
        if weather_output:
            usage = RequestUsage(input_tokens=0, output_tokens=0)
            if chunk_queue is not None:
                await chunk_queue.put(weather_output)
            synthetic_messages = [
                ModelRequest(parts=[UserPromptPart(content=user_message)]),
                ModelResponse(
                    parts=[TextPart(content=weather_output)],
                    usage=usage,
                    model_name=decision.model_name,
                ),
            ]
            synthetic_result = _BedrockRunResult(
                output_text=weather_output,
                messages=synthetic_messages,
                usage=usage,
            )

            lf_update_current_observation(
                output=weather_output,
                model=decision.model_name,
                input_tokens=0,
                output_tokens=0,
                metadata={**observation_metadata, "provider": "bedrock", "weather_tool_forced": True},
            )
            return _AgrinetCompletedRun(result=synthetic_result, output_text=weather_output)

        output_text, usage = await _call_bedrock_converse(
            model_name=decision.model_name,
            user_message=user_message,
            system_prompt=_build_agrinet_system_prompt(deps),
            temperature=0.7,
            top_p=0.95,
            max_tokens=4096,
            timeout_seconds=settings.agrinet_model_timeout_seconds,
        )

        if chunk_queue is not None:
            await chunk_queue.put(output_text)

        synthetic_messages = [
            ModelRequest(parts=[UserPromptPart(content=user_message)]),
            ModelResponse(
                parts=[TextPart(content=output_text)],
                usage=usage,
                model_name=decision.model_name,
            ),
        ]
        synthetic_result = _BedrockRunResult(output_text=output_text, messages=synthetic_messages, usage=usage)

        lf_update_current_observation(
            output=output_text,
            model=decision.model_name,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            metadata={**observation_metadata, "provider": "bedrock"},
        )
        return _AgrinetCompletedRun(result=synthetic_result, output_text=output_text)

    stream_state = _AgrinetStreamState()
    result = None

    try:
        async with asyncio.timeout(settings.agrinet_model_timeout_seconds):
            async for event in agrinet_agent.run_stream_events(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
                model=get_agrinet_route_model(decision.route),
            ):
                if isinstance(event, FinalResultEvent):
                    stream_state.final_result_found = True
                    continue

                if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    if not stream_state.final_result_found:
                        continue
                    raw_chunk = event.delta.content_delta or ""
                    stream_state.raw_chunks.append(raw_chunk)
                    visible_chunk = _strip_streaming_thinking_chunk(raw_chunk, stream_state)
                    if visible_chunk and chunk_queue is not None:
                        await chunk_queue.put(visible_chunk)
                    continue

                if getattr(event, "event_kind", "") == "function_tool_result":
                    stream_state.final_result_found = False
                    continue

                if isinstance(event, AgentRunResultEvent):
                    result = event.result
    except TimeoutError as exc:
        lf_update_current_observation(
            metadata={
                **observation_metadata,
                "error_type": "TimeoutError",
                "timeout_seconds": settings.agrinet_model_timeout_seconds,
            }
        )
        raise TimeoutError(
            f"Agrinet route {decision.route} timed out after {settings.agrinet_model_timeout_seconds} seconds"
        ) from exc
    except Exception as exc:
        lf_update_current_observation(
            metadata={
                **observation_metadata,
                "error_type": type(exc).__name__,
            }
        )
        raise

    if result is None:
        raise RuntimeError("Agrinet stream finished without a final result")

    fallback_output = str(getattr(result, "output", "") or "")
    streamed_output = _sanitize_streamed_output("".join(stream_state.raw_chunks))
    output_text = streamed_output or _sanitize_streamed_output(fallback_output)

    usage_data = result.usage()
    lf_update_current_observation(
        output=output_text,
        model=decision.model_name,
        input_tokens=usage_data.input_tokens or 0,
        output_tokens=usage_data.output_tokens or 0,
        metadata=observation_metadata,
    )
    return _AgrinetCompletedRun(result=result, output_text=output_text)


@observe(name=AGENT_VISTAAR, as_type="agent")
async def _run_agrinet_once(
    user_message: str,
    trimmed_history: list,
    deps: FarmerContext,
    session_id: str,
    user_id: str,
    moderation_category: str,
    decision: AgrinetRouteDecision,
    fallback_used: bool,
    fallback_from: AgrinetRoute | None = None,
):
    """Run the agrinet agent for a specific model route and trace it in Langfuse."""
    route_metadata = _agrinet_route_metadata(
        decision,
        fallback_used=fallback_used,
        fallback_from=fallback_from,
    )
    observation_metadata = {
        "session_id": session_id,
        "user_id": user_id,
        "moderation_category": moderation_category,
        **route_metadata,
    }

    lf_update_current_observation(
        input=user_message,
        model=decision.model_name,
        metadata=observation_metadata,
    )

    if _is_bedrock_mode():
        weather_output = await _try_weather_tool_response(deps.query, deps.farmer_coordinates)
        if weather_output:
            usage = RequestUsage(input_tokens=0, output_tokens=0)
            synthetic_messages = [
                ModelRequest(parts=[UserPromptPart(content=user_message)]),
                ModelResponse(
                    parts=[TextPart(content=weather_output)],
                    usage=usage,
                    model_name=decision.model_name,
                ),
            ]
            synthetic_result = _BedrockRunResult(
                output_text=weather_output,
                messages=synthetic_messages,
                usage=usage,
            )
            lf_update_current_observation(
                output=weather_output,
                model=decision.model_name,
                input_tokens=0,
                output_tokens=0,
                metadata={**observation_metadata, "provider": "bedrock", "weather_tool_forced": True},
            )
            return synthetic_result

        output_text, usage = await _call_bedrock_converse(
            model_name=decision.model_name,
            user_message=user_message,
            system_prompt=_build_agrinet_system_prompt(deps),
            temperature=0.7,
            top_p=0.95,
            max_tokens=4096,
            timeout_seconds=settings.agrinet_model_timeout_seconds,
        )
        synthetic_messages = [
            ModelRequest(parts=[UserPromptPart(content=user_message)]),
            ModelResponse(
                parts=[TextPart(content=output_text)],
                usage=usage,
                model_name=decision.model_name,
            ),
        ]
        synthetic_result = _BedrockRunResult(output_text=output_text, messages=synthetic_messages, usage=usage)
        lf_update_current_observation(
            output=output_text,
            model=decision.model_name,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            metadata={**observation_metadata, "provider": "bedrock"},
        )
        return synthetic_result

    try:
        result = await asyncio.wait_for(
            agrinet_agent.run(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
                model=get_agrinet_route_model(decision.route),
            ),
            timeout=settings.agrinet_model_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        lf_update_current_observation(
            metadata={
                **observation_metadata,
                "error_type": "TimeoutError",
                "timeout_seconds": settings.agrinet_model_timeout_seconds,
            }
        )
        raise TimeoutError(
            f"Agrinet route {decision.route} timed out after {settings.agrinet_model_timeout_seconds} seconds"
        ) from exc
    except Exception as exc:
        lf_update_current_observation(
            metadata={
                **observation_metadata,
                "error_type": type(exc).__name__,
            }
        )
        raise

    usage_data = result.usage()
    lf_update_current_observation(
        output=result.output,
        model=decision.model_name,
        input_tokens=usage_data.input_tokens or 0,
        output_tokens=usage_data.output_tokens or 0,
        metadata=observation_metadata,
    )
    return result