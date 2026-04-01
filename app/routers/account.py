import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from utils.auth import AuthError, create_jwt, verify_google_token
from app.config import SANDBOX_NAMESPACE
from app.dependencies import require_auth
from app.models.schemas import CreateApiKeyRequest, GoogleAuthRequest
from utils.db import create_api_key, delete_api_key, get_or_create_user, get_workspace_by_user_id, list_api_keys

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

# TODO: This is a temporary endpoint to create a workspace for the user. Mostly for demo.
@router.post("/account-with-workspace")
async def create_or_get_account_with_workspace(req: GoogleAuthRequest):
    """Verify Google id_token, create or get user, return JWT."""
    try:
        google_user = verify_google_token(req.id_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    user = await get_or_create_user("google", google_user["sub"], google_user["email"])
    token = create_jwt(str(user.id))

    workspace = await get_workspace_by_user_id(user.id)
    workspace_data = None
    if workspace:
        workspace_data = {
            "claim_name": workspace.claim_name,
            "template_name": workspace.template_name,
            "namespace": SANDBOX_NAMESPACE,
        }

    return {"user_id": str(user.id), "token": token, "workspace": workspace_data}


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


@router.get("/account/api-keys", dependencies=[Depends(require_auth)])
async def list_api_keys_endpoint(request: Request):
    """List all API keys for the authenticated user (masked keys only)."""
    user_id = uuid.UUID(request.state.user_id)
    keys = await list_api_keys(user_id)
    return [
        {
            "id": str(k.id),
            "key_masked": k.key_masked,
            "name": k.name,
            "created_at": k.created_at.isoformat(),
        }
        for k in keys
    ]


@router.delete("/account/api-keys/{api_key_id}", dependencies=[Depends(require_auth)])
async def delete_api_key_endpoint(request: Request, api_key_id: str):
    """Delete an API key by ID for the authenticated user."""
    user_id = uuid.UUID(request.state.user_id)
    deleted = await delete_api_key(uuid.UUID(api_key_id), user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"deleted": True}

