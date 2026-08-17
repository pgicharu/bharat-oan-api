from pydantic import BaseModel, Field, field_validator
from typing import Any, Dict, Optional, Literal

SESSION_ID_DESCRIPTION = "Session ID"

class ChatRequest(BaseModel):
    query: str = Field(..., description="The user's chat query")
    session_id: Optional[str] = Field(None, description="Session ID for maintaining conversation context")
    qid: Optional[str] = Field(None, description="Question ID for telemetry correlation")
    source_lang: str = Field('hi', description="Source language code")
    target_lang: str = Field('hi', description="Target language code")
    user_id: str = Field('anonymous', description="User identifier")
    latitude: Optional[str] = Field(None, description="Request-time latitude")
    longitude: Optional[str] = Field(None, description="Request-time longitude")
    farmer_token: Optional[str] = Field(None, description="Opaque farmer-registry identifier for advisory context enrichment")

class TelemetryFeedbackRequest(BaseModel):
    qid: str = Field(..., description="Question or message ID for telemetry correlation")
    session_id: str = Field(..., description=SESSION_ID_DESCRIPTION)
    message_id: Optional[str] = Field(None, description="Assistant message ID")
    feedback_type: str = Field(..., description="Feedback type, e.g. like or dislike")
    feedback_text: str = Field("", description="Feedback text")
    question_text: str = Field("", description="Question text")
    answer_text: str = Field("", description="Answer text")

class TelemetryErrorRequest(BaseModel):
    qid: str = Field(..., description="Question ID for telemetry correlation")
    session_id: str = Field(..., description=SESSION_ID_DESCRIPTION)
    error_text: str = Field(..., description="Error text")
    question_text: Optional[str] = Field(None, description="Question text")
    message_id: Optional[str] = Field(None, description="Related message ID")

class GenericTelemetryEventRequest(BaseModel):
    event_name: str = Field(..., description="Client telemetry event name")
    category: str = Field(..., description="Client telemetry event category")
    time: str = Field(..., description="Client event timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Client event metadata")

    @field_validator("event_name", "category")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

class TranscribeRequest(BaseModel):
    audio_content: str = Field(..., description="Base64 encoded audio content")
    lang_code: str = Field(..., description="Language code for transcription")
    service_type: Literal['bhashini', 'whisper'] = Field('bhashini', description="Transcription service to use")
    session_id: Optional[str] = Field(None, description=SESSION_ID_DESCRIPTION)
    qid: Optional[str] = Field(None, description="Question ID")

class SuggestionsRequest(BaseModel):
    session_id: str = Field(..., description="Session ID to get suggestions for")
    target_lang: str = Field('hi', description="Target language for suggestions")

class TTSRequest(BaseModel):
    text: str = Field(..., description="Text to convert to speech")
    target_lang: str = Field('hi', description="Language code for TTS")
    session_id: Optional[str] = Field(None, description=SESSION_ID_DESCRIPTION)
    service_type: Literal['bhashini', 'eleven_labs'] = Field('bhashini', description="TTS service to use")
    qid: Optional[str] = Field(None, description="Question ID") 


class FileRequest(BaseModel):
    file_uuid: str = Field(..., description="File UUID")
    session_id: Optional[str] = Field(None, description=SESSION_ID_DESCRIPTION)

class FarmerRegistryLoginRequest(BaseModel):
    phone: str = Field(..., description="Phone number registered with the farmer registry")
    password: str = Field(..., description="Farmer registry account password")
