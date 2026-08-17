"""
Server-to-server proxy for the farmers-registry self-service login.

The UI never talks to farmers-registry directly: its X-API-Key is a backend
secret, and the provider has no CORS configured for a public origin (see
providers/04_FARMER_REGISTRY_NETWORK_ONBOARDING.md). This router requires
the caller to already hold a valid bharat-oan-api session — it is an
account-linking action ("connect your farmer registry profile"), not a
second, independently-guessable login surface — then verifies the farmer's
credentials against providers/farmers-registry's POST /auth/verify and hands
back only the opaque farmer_token for the client to store and resend as
ChatRequest.farmer_token.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.auth.jwt_auth import get_current_user
from app.config import DEFAULT_HTTP_TIMEOUT
from app.models.requests import FarmerRegistryLoginRequest
from app.models.responses import FarmerRegistryLoginResponse
from helpers.utils import get_logger

router = APIRouter(prefix="/farmer-auth", tags=["farmer-auth"])
logger = get_logger(__name__)


@router.post("/login", response_model=FarmerRegistryLoginResponse)
async def farmer_registry_login(
    request: FarmerRegistryLoginRequest,
    current_user: dict = Depends(get_current_user),
) -> FarmerRegistryLoginResponse:
    base_url = os.getenv("FARMERS_REGISTRY_BASE_URL")
    api_key = os.getenv("FARMERS_REGISTRY_API_KEY")
    if not base_url or not api_key:
        logger.error("FARMERS_REGISTRY_BASE_URL / FARMERS_REGISTRY_API_KEY is not set")
        raise HTTPException(status_code=503, detail="Farmer registry login is not configured")

    verify_url = base_url.rstrip("/") + "/auth/verify"
    try:
        response = httpx.post(
            verify_url,
            json={"phone": request.phone, "password": request.password},
            headers={"X-API-Key": api_key},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.RequestError as e:
        logger.error(f"Farmer registry auth request failed: {e}")
        raise HTTPException(status_code=502, detail="Farmer registry is unavailable") from e

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid phone or password")
    if not response.is_success:
        logger.error(
            "Farmer registry auth returned status %s for URL %s — response: %s",
            response.status_code,
            verify_url,
            response.text[:500] if response.text else "(empty)",
        )
        raise HTTPException(status_code=502, detail="Farmer registry login failed")

    body = response.json()
    data = body.get("data") or {}
    farmer_token = data.get("farmer_token")
    farmer_id = data.get("farmer_id")
    if not farmer_token or not farmer_id:
        logger.error("Farmer registry auth response missing farmer_token/farmer_id: %s", body)
        raise HTTPException(status_code=502, detail="Farmer registry returned an unexpected response")

    logger.info(
        "Farmer registry login linked for user_id=%s farmer_id=%s",
        current_user.get("sub") or current_user.get("mobile"),
        farmer_id,
    )
    return FarmerRegistryLoginResponse(farmer_id=farmer_id, farmer_token=farmer_token)
