import uuid

from agentic_sandbox import SandboxClient
from fastapi import APIRouter, HTTPException

from utils.auth import AuthError, create_jwt, verify_google_token
from app.config import (
    CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME,
    SANDBOX_API_URL,
    SANDBOX_NAMESPACE,
)
from app.dependencies import workspaces
from app.models.schemas import GoogleAuthRequest
from app.services.sandbox import reconstruct_sandbox
from utils.db import create_workspace_record, get_or_create_user, get_workspace_by_user_id

router = APIRouter()


@router.post("/auth/google")
async def auth_google(req: GoogleAuthRequest):
    try:
        google_user = verify_google_token(req.id_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    user = await get_or_create_user(google_user["sub"], google_user["email"])

    existing_ws = await get_workspace_by_user_id(user.id)
    if existing_ws:
        workspace_id = str(existing_ws.id)
        # Reconnect sandbox if not in memory cache
        if workspace_id not in workspaces:
            sandbox = reconstruct_sandbox(existing_ws.claim_name, existing_ws.template_name)
            workspaces[workspace_id] = sandbox
    else:
        # Create a new sandbox and persist
        workspace_id = str(uuid.uuid4())
        sandbox = SandboxClient(
            template_name=CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME,
            namespace=SANDBOX_NAMESPACE,
            api_url=SANDBOX_API_URL,
        )
        sandbox.__enter__()
        workspaces[workspace_id] = sandbox

        await create_workspace_record(
            user_id=user.id,
            workspace_id=uuid.UUID(workspace_id),
            claim_name=sandbox.claim_name,
            template_name=CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME,
        )

    token = create_jwt(str(user.id), workspace_id)
    return {"workspace_id": workspace_id, "token": token}
