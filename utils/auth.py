import os
from datetime import datetime, timedelta, timezone

import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def verify_google_token(token: str) -> dict:
    """Validate a Google ID token and return user info.

    Returns:
        dict with "sub" (Google user ID) and "email".

    Raises:
        AuthError on invalid/expired token.
    """
    try:
        idinfo = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
        return {"sub": idinfo["sub"], "email": idinfo["email"]}
    except ValueError as exc:
        raise AuthError(f"Invalid Google token: {exc}") from exc


def create_jwt(user_id: str, workspace_id: str) -> str:
    """Issue a JWT containing user_id and workspace_id."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "workspace_id": workspace_id,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decode and validate a JWT.

    Returns:
        The decoded payload dict.

    Raises:
        AuthError on invalid/expired token.
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc
