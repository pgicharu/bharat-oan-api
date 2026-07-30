import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai import AsyncAzureOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings

load_dotenv()

AgrinetRoute = Literal["gpt41", "gemma"]
AGRINET_DEFAULT_ROUTE: AgrinetRoute = "gpt41"


@lru_cache(maxsize=1)
def _get_default_azure_client() -> AsyncAzureOpenAI:
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_api_version = os.getenv("AZURE_OPENAI_API_VERSION")

    if not azure_endpoint:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is required")
    if not azure_api_key:
        raise ValueError("AZURE_OPENAI_API_KEY environment variable is required")
    if not azure_api_version:
        raise ValueError("AZURE_OPENAI_API_VERSION environment variable is required")

    return AsyncAzureOpenAI(
        azure_endpoint=azure_endpoint.rstrip("/"),
        api_version=azure_api_version,
        api_key=azure_api_key,
    )


@lru_cache(maxsize=1)
def _get_default_bedrock_client() -> AsyncOpenAI:
    base_url = (os.getenv("BEDROCK_BASE_URL") or "").strip()
    api_key = (os.getenv("BEDROCK_API_KEY") or os.getenv("BEDROCK_TOKEN") or "").strip()
    auth_header = (os.getenv("BEDROCK_AUTH_HEADER") or "Authorization").strip()
    auth_scheme = os.getenv("BEDROCK_AUTH_SCHEME")
    auth_scheme = "Bearer" if auth_scheme is None else auth_scheme.strip()

    if not base_url:
        raise ValueError("BEDROCK_BASE_URL environment variable is required")
    if not api_key:
        raise ValueError("BEDROCK_API_KEY or BEDROCK_TOKEN environment variable is required")

    if auth_scheme.lower() == "none":
        auth_scheme = ""

    header_value = f"{auth_scheme} {api_key}" if auth_scheme else api_key

    return AsyncOpenAI(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        default_headers={auth_header: header_value},
    )


# Keep the primary agrinet + moderation wiring close to the original layout so
# the new routing behavior adds as little mental overhead as possible.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()

if LLM_PROVIDER == "vllm":
    agrinet_model_name = os.getenv("LLM_AGRINET_MODEL_NAME", "agrinet-model")
    moderation_model_name = os.getenv("LLM_MODERATION_MODEL_NAME", "moderation-model")
    vllm_agrinet_url = os.getenv("VLLM_AGRINET_MODEL_URL")
    vllm_moderation_url = os.getenv("VLLM_MODERATION_MODEL_URL", vllm_agrinet_url)

    if not vllm_agrinet_url:
        raise ValueError("VLLM_AGRINET_MODEL_URL is required when using vllm provider")

    AGRINET_MODEL = OpenAIChatModel(
        agrinet_model_name,
        provider=OpenAIProvider(
            base_url=vllm_agrinet_url,
            api_key="not-needed",
        ),
    )
    MODERATION_MODEL = OpenAIChatModel(
        moderation_model_name,
        provider=OpenAIProvider(
            base_url=vllm_moderation_url,
            api_key="not-needed",
        ),
    )
elif LLM_PROVIDER == "openai":
    AGRINET_MODEL = OpenAIChatModel(
        os.getenv("LLM_AGRINET_MODEL_NAME", "gpt-3.5-turbo"),
        provider=OpenAIProvider(
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
    )
    MODERATION_MODEL = OpenAIChatModel(
        os.getenv("LLM_MODERATION_MODEL_NAME", "gpt-3.5-turbo"),
        provider=OpenAIProvider(
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
    )
elif LLM_PROVIDER == "azure-openai":
    azure_deployment_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

    if not azure_deployment_name:
        raise ValueError("AZURE_OPENAI_DEPLOYMENT_NAME environment variable is required")

    agrinet_deployment = os.getenv("LLM_AGRINET_MODEL_NAME", azure_deployment_name)
    moderation_deployment = os.getenv("LLM_MODERATION_MODEL_NAME", azure_deployment_name)
    azure_client = _get_default_azure_client()

    AGRINET_MODEL = OpenAIChatModel(
        agrinet_deployment,
        provider=OpenAIProvider(openai_client=azure_client),
    )
    MODERATION_MODEL = OpenAIChatModel(
        moderation_deployment,
        provider=OpenAIProvider(
            openai_client=azure_client,
        ),
    )
elif LLM_PROVIDER == "bedrock":
    bedrock_model_name = os.getenv("LLM_AGRINET_MODEL_NAME") or os.getenv("BEDROCK_MODEL_NAME")
    moderation_model_name = os.getenv("LLM_MODERATION_MODEL_NAME", bedrock_model_name)

    if not bedrock_model_name:
        raise ValueError(
            "LLM_AGRINET_MODEL_NAME or BEDROCK_MODEL_NAME environment variable is required"
        )

    bedrock_client = _get_default_bedrock_client()

    AGRINET_MODEL = OpenAIChatModel(
        bedrock_model_name,
        provider=OpenAIProvider(openai_client=bedrock_client),
    )
    MODERATION_MODEL = OpenAIChatModel(
        moderation_model_name,
        provider=OpenAIProvider(openai_client=bedrock_client),
    )
else:
    raise ValueError(
        f"Invalid LLM_PROVIDER: {LLM_PROVIDER}. Must be one of: 'vllm', 'openai', 'azure-openai', 'bedrock'"
    )


@lru_cache(maxsize=1)
def _build_gemma_agrinet_model() -> OpenAIChatModel:
    provider = (settings.agrinet_gemma_provider or "vllm").strip().lower()
    if provider != "vllm":
        raise ValueError(
            f"Unsupported AGRINET_GEMMA_PROVIDER: {provider}. Only 'vllm' is supported."
        )

    model_name = (settings.agrinet_gemma_model_name or "").strip()
    if not model_name:
        raise ValueError("AGRINET_GEMMA_MODEL_NAME environment variable is required")

    base_url = (settings.agrinet_gemma_base_url or "").strip()
    if not base_url:
        raise ValueError("AGRINET_GEMMA_BASE_URL environment variable is required")

    api_key = (settings.agrinet_gemma_api_key or "not-needed").strip() or "not-needed"

    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
        ),
    )


def get_agrinet_route_model(route: AgrinetRoute) -> OpenAIChatModel:
    if route == "gpt41":
        return AGRINET_MODEL
    if route == "gemma":
        return _build_gemma_agrinet_model()
    raise ValueError(f"Unsupported agrinet route: {route}")


def get_agrinet_route_model_name(route: AgrinetRoute) -> str:
    return get_agrinet_route_model(route).model_name


def validate_agrinet_routing_config() -> None:
    if not settings.agrinet_routing_enabled:
        return

    weights = (
        settings.agrinet_route_gpt41_weight,
        settings.agrinet_route_gemma_weight,
    )
    if any(weight < 0 for weight in weights):
        raise ValueError("AGRINET routing weights must be non-negative integers")
    if sum(weights) != 100:
        raise ValueError("AGRINET routing weights must sum to 100")
    if settings.agrinet_route_ttl_seconds <= 0:
        raise ValueError("AGRINET_ROUTE_TTL_SECONDS must be a positive integer")

    # Force Gemma config validation during startup when routing is enabled.
    get_agrinet_route_model("gemma")


# Langfuse generation `model` field (providedModelName) for dashboard model breakdown.
LANGFUSE_AGRINET_MODEL_NAME = AGRINET_MODEL.model_name
LANGFUSE_MODERATION_MODEL_NAME = MODERATION_MODEL.model_name