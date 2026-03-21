from fastapi import APIRouter, HTTPException

from utils.auth import AuthError, create_jwt, verify_google_token
from app.models.schemas import GoogleAuthRequest
from utils.db import get_or_create_user

router = APIRouter()


@router.post("/account")
async def create_account(req: GoogleAuthRequest):
    """Verify Google id_token, create or get user, return JWT."""
    try:
        google_user = verify_google_token(req.id_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    user = await get_or_create_user("google", google_user["sub"], google_user["email"])
    token = create_jwt(str(user.id))
    return {"user_id": str(user.id), "token": token}

