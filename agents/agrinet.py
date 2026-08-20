import os
from pydantic_ai import Agent, RunContext
from helpers.utils import get_prompt, get_today_date_str, get_crop_season
from agents.models import AGRINET_MODEL
from agents.tools import TOOLS
from pydantic_ai.models.openai import OpenAIChatModelSettings
from agents.deps import FarmerContext


agrinet_agent = Agent(
    model=AGRINET_MODEL,
    name="Vistaar Agent",
    instrument=False,
    output_type=str,
    deps_type=FarmerContext,
    retries=3,
    tools=TOOLS,
    end_strategy='exhaustive',
    model_settings=OpenAIChatModelSettings(
        temperature=0.7,
        top_p=0.95,
        max_tokens=4096,
        timeout=120,
        parallel_tool_calls=True,
    )
)

@agrinet_agent.system_prompt(dynamic=True)
def get_system_prompt(ctx: RunContext[FarmerContext]):
    """Get the system prompt for the agrinet agent."""
    return get_prompt('agrinet_en', context={'today_date': get_today_date_str(), 'crop_season': get_crop_season()})