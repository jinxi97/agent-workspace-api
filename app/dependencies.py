from agentic_sandbox import SandboxClient
from fastapi import HTTPException, Request

from utils.auth import AuthError, decode_jwt
from utils.db import verify_api_key

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


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that validates an API key from the X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    record = await verify_api_key(api_key)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    request.state.api_key_user_id = str(record.user_id)


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
