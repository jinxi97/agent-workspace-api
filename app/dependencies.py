from agentic_sandbox import SandboxClient
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from utils.auth import AuthError, decode_jwt
from utils.db import verify_api_key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)

WORKSPACE_TIMEOUT_SECONDS = 300
RESTORE_TIMEOUT_SECONDS = 300
SNAPSHOT_TIMEOUT_SECONDS = 300


async def require_auth(request: Request) -> dict:
    """FastAPI dependency that validates JWT and returns claims.

    Sets request.state.user_id and request.state.workspace_id as a
    convenience for handlers that receive the Request directly.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[len("Bearer "):]
    try:
        claims = decode_jwt(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    request.state.user_id = claims["sub"]
    request.state.workspace_id = claims.get("workspace_id")
    return claims


async def require_api_key(
    request: Request,
    api_key: str | None = Security(_api_key_header),
) -> None:
    """FastAPI dependency that validates an API key from the X-API-Key header."""
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    record = await verify_api_key(api_key)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    request.state.api_key_user_id = str(record.user_id)


async def require_any_auth(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    """FastAPI dependency that accepts either JWT (Bearer) or API key (X-API-Key).

    Sets request.state.user_id as a string regardless of which method is used.
    """

    if api_key:
        record = await verify_api_key(api_key)
        if not record:
            raise HTTPException(status_code=401, detail="Invalid API key")
        request.state.user_id = str(record.user_id)
        return

    if bearer:
        try:
            claims = decode_jwt(bearer.credentials)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=exc.message) from exc
        request.state.user_id = claims["sub"]
        return

    raise HTTPException(status_code=401, detail="Missing authentication: provide X-API-Key or Authorization header")


def create_sandbox(claim_name: str, namespace: str, pod_name: str) -> SandboxClient:
    """Create a SandboxClient from the provided params. No caching, no I/O."""
    from app.config import SANDBOX_API_URL

    sandbox = SandboxClient(
        template_name="",
        namespace=namespace,
        api_url=SANDBOX_API_URL,
    )
    sandbox.claim_name = claim_name
    sandbox.sandbox_name = claim_name
    sandbox.pod_name = pod_name
    return sandbox
