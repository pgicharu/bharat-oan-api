"""
Tools for the BharatVistaar AI Agent.
"""
# from agents.tools.search_beckn import search_documents
from pydantic_ai import Tool
from agents.tools.scheme_info import get_scheme_info
from agents.tools.pmkisan_scheme_status import initiate_pm_kisan_status_check, check_pm_kisan_status_with_otp
from agents.tools.pmfby_scheme_status import initiate_pmfby_status_check, check_pmfby_status_with_otp
from agents.tools.pmfby_grievance import (
    initiate_pmfby_grievance_otp,
    check_pmfby_grievance_otp,
    pmfby_grievance_status,
    pmfby_submit_grievance,
)
from agents.tools.shc_scheme_status import check_shc_status
from agents.tools.pmkisan_grievance import submit_pmkisan_grievance, pmkisan_grievance_status
from agents.tools.terms import search_terms
# Marqo-backed advisory search (search_documents / search_videos /
# search_pests_diseases) is intentionally not registered: all agricultural
# advisory now routes through `knowledge_advisory`.
from agents.tools.search import search_schemes
from agents.tools.weather import weather_forecast
from agents.tools.knowledge_advisory import knowledge_advisory
# farmer_registry_lookup is intentionally not registered here — the model
# reliably skipped calling it even with explicit prompt instructions.
# agents.tools.farmer_registry.fetch_farmer_profile_context is called
# deterministically from app.services.chat before the agent runs instead;
# see that module's docstring.
from agents.tools.mandi import get_mandi_prices
from agents.tools.commodity import search_commodity
from agents.tools.maps import reverse_geocode, forward_geocode
from agents.tools.agrovets import search_agrovets
from agents.tools.food_prices import search_food_prices
from agents.tools.soil_data import get_soil_data
from agents.tools.tractor_operators import search_tractor_operators
from agents.tools.smam_scheme_status import check_smam_scheme_status
from agents.tools.gfr import gfr_get_recommendations, gfr_get_crop_registries
from agents.tools.sathi_seed import (
    get_sathi_crop_groups,
    list_sathi_crops_in_group,
    search_sathi_seed_availability,
)

TOOLS = [
    Tool(
        get_scheme_info,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        initiate_pm_kisan_status_check,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pm_kisan_status_with_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        initiate_pmfby_status_check,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pmfby_status_with_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        initiate_pmfby_grievance_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_pmfby_grievance_otp,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmfby_grievance_status,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        pmfby_submit_grievance,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        check_shc_status,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        submit_pmkisan_grievance,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        pmkisan_grievance_status,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_terms,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_schemes,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        weather_forecast,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        knowledge_advisory,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        get_mandi_prices,
        takes_ctx=True,
        strict=False,
    ),
    Tool(
        search_commodity,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        forward_geocode,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        reverse_geocode,
        takes_ctx=False,    
        strict=False,
    ),
    Tool(
        gfr_get_recommendations,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        gfr_get_crop_registries,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        get_sathi_crop_groups,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        list_sathi_crops_in_group,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_sathi_seed_availability,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        check_smam_scheme_status,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_agrovets,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_food_prices,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        get_soil_data,
        takes_ctx=False,
        strict=False,
    ),
    Tool(
        search_tractor_operators,
        takes_ctx=False,
        strict=False,
    ),
]
