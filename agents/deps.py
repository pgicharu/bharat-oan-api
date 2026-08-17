from typing import Optional, Tuple
from pydantic import BaseModel, Field, field_validator
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        session_id (str): The session ID for the conversation.
        moderation_str (Optional[str]): The moderation result of the user's question.


    Example:
        **User:** "What is the weather in Mumbai?"
        **Selected Language:** Hindi
        **Moderation Compliance:** "Valid Agricultural (This is a valid agricultural question.)"
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='hi')
    session_id: str = Field(description="The session ID for the conversation.")
    moderation_str: Optional[str] = Field(default=None, description="The moderation result of the user's question.")
    farmer_token: Optional[str] = Field(
        default=None,
        description=(
            "Opaque farmer-registry identifier for the current session, supplied by the "
            "frontend. Used by app.services.chat to pre-fetch farmer_profile_context "
            "before the agent runs — never exposed to the model directly."
        ),
    )
    farmer_profile_context: Optional[str] = Field(
        default=None,
        description=(
            "Pre-fetched farmer advisory context (county, crops, soil, irrigation, "
            "resolved coordinates), fetched once via "
            "agents.tools.farmer_registry.fetch_farmer_profile_context before the agent "
            "runs, and folded into get_user_message() below — see that module's "
            "docstring for why this isn't an agent-callable tool. None if no "
            "farmer_token was supplied for this session."
        ),
    )
    farmer_coordinates: Optional[Tuple[float, float]] = Field(
        default=None,
        description=(
            "The same (lat, lon) embedded in farmer_profile_context's 'Coordinates' "
            "line, kept as a separate structured field so app.services.chat's "
            "deterministic weather-location resolution (_resolve_weather_coordinates) "
            "can use it as a fallback without re-parsing the rendered text. None "
            "whenever farmer_profile_context is None or the farmer's county didn't "
            "resolve to a known coordinate."
        ),
    )

    def update_moderation_str(self, moderation_str: str):
        """Update the moderation result of the user's question."""
        self.moderation_str = moderation_str

    def update_farmer_profile(
        self,
        farmer_profile_context: Optional[str],
        farmer_coordinates: Optional[Tuple[float, float]] = None,
    ):
        """Set the pre-fetched farmer profile context and its resolved coordinates."""
        self.farmer_profile_context = farmer_profile_context
        self.farmer_coordinates = farmer_coordinates

    def _language_string(self):
        """Get the language string for the agrinet agent."""
        if self.lang_code:
            return f"**Selected Language:** {Language.get(self.lang_code).display_name()}"
        else:
            return None
    
    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def _moderation_string(self):
        """Get the moderation string for the agrinet agent."""
        if self.moderation_str:
            return self.moderation_str
        else:
            return None
    
    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        strings = [
            self._query_string(),
            self._language_string(),
            self._moderation_string(),
            self.farmer_profile_context,
        ]
        return "\n".join([x for x in strings if x])