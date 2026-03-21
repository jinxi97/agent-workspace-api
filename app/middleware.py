from fastapi import Request
from fastapi.responses import JSONResponse

from utils.auth import AuthError, decode_jwt
from app.config import AUTH_EXEMPT_PATHS, AUTH_EXEMPT_PREFIXES, WORKSPACE_PATH_PATTERN


async def require_auth(request: Request, call_next):
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or path in AUTH_EXEMPT_PATHS
        or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)
    ):
        return await call_next(request)

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid Authorization header"})

    token = auth_header[len("Bearer "):]
    try:
        claims = decode_jwt(token)
    except AuthError as exc:
        return JSONResponse(status_code=401, content={"detail": exc.message})

    request.state.user_id = claims["sub"]
    request.state.workspace_id = claims["workspace_id"]

    # Check workspace ownership for /workspaces/{id}/... routes
    match = WORKSPACE_PATH_PATTERN.match(request.url.path)
    if match:
        path_workspace_id = match.group(1)
        if path_workspace_id != claims["workspace_id"]:
            return JSONResponse(
                status_code=403,
                content={"detail": "You do not have access to this workspace"},
            )

    return await call_next(request)
