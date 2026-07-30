from __future__ import annotations

from typing import Any, Dict, Tuple

from fastapi import APIRouter, Request

from app.utils import set_cache
from helpers.utils import get_logger

router = APIRouter(prefix="/bap-webhook", tags=["bap-webhook"])
logger = get_logger(__name__)

CALLBACK_TTL_SECONDS = 60 * 30


def _extract_ids(payload: Dict[str, Any]) -> Tuple[str | None, str | None]:
    context = payload.get("context") or {}
    transaction_id = context.get("transaction_id") or context.get("transactionId")
    message_id = context.get("message_id") or context.get("messageId")
    return transaction_id, message_id


async def _store_callback_payload(payload: Dict[str, Any], request_path: str) -> None:
    transaction_id, message_id = _extract_ids(payload)

    # Store most-recent callback globally for quick diagnostics.
    await set_cache("beckn:on_search:last", payload, ttl=CALLBACK_TTL_SECONDS)

    if transaction_id:
        await set_cache(
            f"beckn:on_search:txn:{transaction_id}",
            payload,
            ttl=CALLBACK_TTL_SECONDS,
        )
    if message_id:
        await set_cache(
            f"beckn:on_search:msg:{message_id}",
            payload,
            ttl=CALLBACK_TTL_SECONDS,
        )

    logger.info(
        "Received Beckn callback path=%s action=%s transaction_id=%s message_id=%s",
        request_path,
        (payload.get("context") or {}).get("action"),
        transaction_id,
        message_id,
    )


@router.post("/on_search")
async def on_search_webhook(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    await _store_callback_payload(payload, request.url.path)
    return {"message": {"ack": {"status": "ACK"}}}


@router.post("")
async def generic_bap_webhook(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    await _store_callback_payload(payload, request.url.path)
    return {"message": {"ack": {"status": "ACK"}}}
