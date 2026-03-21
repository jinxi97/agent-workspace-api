import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from utils.auth import AuthError, create_jwt, verify_google_token
from app.dependencies import require_auth
from app.models.schemas import CreateApiKeyRequest, GoogleAuthRequest
from utils.db import create_api_key, get_or_create_user

router = APIRouter()


@router.post("/account")
async def create_or_get_account(req: GoogleAuthRequest):
    """Verify Google id_token, create or get user, return JWT."""
    try:
        google_user = verify_google_token(req.id_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    user = await get_or_create_user("google", google_user["sub"], google_user["email"])
    token = create_jwt(str(user.id))
    return {"user_id": str(user.id), "token": token}


@router.post("/account/api-keys", status_code=201, dependencies=[Depends(require_auth)])
async def create_api_key_endpoint(request: Request, req: CreateApiKeyRequest):
    """Create a new API key for the authenticated user.

    The raw key is returned only once — store it securely.
    """
    user_id = uuid.UUID(request.state.user_id)
    api_key, raw_key = await create_api_key(user_id, name=req.name)
    return {
        "id": str(api_key.id),
        "key": raw_key,
        "created_at": api_key.created_at.isoformat(),
    }

