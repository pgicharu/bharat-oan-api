from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.auth.jwt_auth import get_current_user
from app.services.chat import stream_chat_messages
from app.utils import _get_message_history
from app.models.requests import ChatRequest
from helpers.utils import get_logger
import uuid

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

@router.get("/")
async def chat_endpoint(
    background_tasks: BackgroundTasks,
    request: ChatRequest = Depends(),
    current_user: dict = Depends(get_current_user),  # Authentication required
):
    """
    Chat endpoint that streams responses back to the client.
    Requires JWT authentication.
    """
    session_id = request.session_id or str(uuid.uuid4())
    qid = str(uuid.uuid4())
    channel = current_user.get("channel", "BharatVistaar")
    authenticated_user = current_user.get("mobile")
    logger.info(
        f"Chat request received - session_id: {session_id}, qid: {qid}, user_id: {request.user_id}, "
        f"authenticated_user: {authenticated_user}, channel: {channel}, source_lang: {request.source_lang}, "
        f"target_lang: {request.target_lang}, query: {request.query}"
    )
    
    history = await _get_message_history(session_id)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
        
    return StreamingResponse(
        stream_chat_messages(
            query=request.query,
            session_id=session_id,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            user_id=request.user_id,
            history=history,
            background_tasks=background_tasks,
            channel=channel,
            qid=qid,
            current_user=current_user,
            farmer_token=request.farmer_token,
        ),
        media_type="text/event-stream",
        headers={
            "X-QID": qid,
            "Access-Control-Expose-Headers": "X-QID",
        },
        background=background_tasks,
    )