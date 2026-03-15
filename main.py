import os
import re
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from agentic_sandbox import SandboxClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field

from auth import AuthError, create_jwt, decode_jwt, verify_google_token
from db import (
    close_db,
    create_workspace_record,
    get_or_create_user,
    get_workspace_by_user_id,
    init_db,
)

load_dotenv()

SANDBOX_TEMPLATE_NAME = os.getenv("SANDBOX_TEMPLATE_NAME", "python-runtime-template")
CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME = os.getenv("CLAUDE_AGENT_SANDBOX_TEMPLATE_NAME", "claude-agent-sandbox-template")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "pod-snapshots-ns")
SANDBOX_API_URL = os.getenv(
    "SANDBOX_API_URL",
    "http://sandbox-router-svc.agent-sandbox-application.svc.cluster.local:8080",
)
DATABASE_URL = os.getenv("DATABASE_URL", "")

SNAPSHOT_NAMESPACE = os.getenv("SNAPSHOT_NAMESPACE", SANDBOX_NAMESPACE)

# Paths that don't require JWT authentication
AUTH_EXEMPT_PATHS = {"/", "/healthz", "/auth/google", "/openapi.json", "/docs", "/redoc"}
# Pattern to extract workspace_id from paths like /workspaces/{uuid}/...
WORKSPACE_PATH_PATTERN = re.compile(r"^/workspaces/([^/]+)")

# Store active workspaces (in-memory cache of SandboxClient instances)
workspaces: dict[str, SandboxClient] = {}


CLOUD_SQL_CONNECTION_NAME = os.getenv("CLOUD_SQL_CONNECTION_NAME", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # If DATABASE_URL is set, use direct connection (local dev).
    # If CLOUD_SQL_CONNECTION_NAME is set, use Cloud SQL Python Connector.
    # If neither, skip DB init (e.g. tests with mocked DB).
    if DATABASE_URL or CLOUD_SQL_CONNECTION_NAME:
        await init_db(DATABASE_URL)
    yield
    await close_db()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "https://slides-agent-client-819221826816.us-central1.run.app", 
        "https://funky.dev",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _reconstruct_sandbox(claim_name: str, template_name: str) -> SandboxClient:
    """Reconstruct a SandboxClient from a persisted claim_name."""
    sandbox = SandboxClient(
        template_name=template_name,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.claim_name = claim_name
    sandbox.base_url = SANDBOX_API_URL
    return sandbox


# ---------------------------------------------------------------------------
# Auth middleware (replaces the old API key middleware)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in AUTH_EXEMPT_PATHS:
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


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., min_length=1)


class SnapshotTriggerRequest(BaseModel):
    workspace_id: str = Field(
        ...,
        min_length=1,
        description="Workspace ID returned by POST /workspaces",
    )


class SnapshotRestoreRequest(BaseModel):
    snapshot_name: str = Field(
        ...,
        min_length=1,
        description="Exact PodSnapshot name to restore from",
    )


class ExecuteRequest(BaseModel):
    workspace_id: str
    command: str


class CreateChatRequest(BaseModel):
    title: str | None = Field(default=None, description="Optional chat title")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Message content to send")


class AnswerRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="Question text → selected option label")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_snapshot_ready(snapshot: dict) -> bool:
    status = snapshot.get("status", {})
    if status.get("phase") == "AllSnapshotsAvailable":
        return True
    conditions = status.get("conditions", [])
    for cond in conditions:
        cond_type = cond.get("type")
        cond_status = cond.get("status")
        if cond_type in {"Ready", "Available"} and cond_status == "True":
            return True
    return False


def _get_ready_snapshot_or_400(api: client.CustomObjectsApi, snapshot_name: str) -> dict:
    try:
        snapshot = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshots",
            name=snapshot_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=400, detail=f"Invalid snapshot_name: {snapshot_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    if not _is_snapshot_ready(snapshot):
        raise HTTPException(
            status_code=400,
            detail=f"Snapshot is not ready for restore: {snapshot_name}",
        )
    return snapshot


def _get_k8s_custom_api() -> client.CustomObjectsApi:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


def _get_sandbox_or_404(workspace_id: str) -> SandboxClient:
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sandbox


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------
@app.post("/auth/google")
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
            sandbox = _reconstruct_sandbox(existing_ws.claim_name, existing_ws.template_name)
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


# ---------------------------------------------------------------------------
# Health / root
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "Hello World!"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Workspace endpoints (sandbox proxy)
# ---------------------------------------------------------------------------
@app.post("/execute")
def exec_command(req: ExecuteRequest):
    sandbox = _get_sandbox_or_404(req.workspace_id)
    result = sandbox.run(req.command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code
    }


@app.post("/workspaces/{workspace_id}/chats", status_code=201)
def create_chat(workspace_id: str, req: CreateChatRequest):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("POST", "api/chats", json={"title": req.title})
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error communicating with sandbox: {exc}",
        ) from exc

    return response.json()


@app.post("/workspaces/{workspace_id}/chats/{chat_id}/messages")
def send_message(workspace_id: str, chat_id: str, req: SendMessageRequest):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request(
            "POST",
            f"api/chats/{chat_id}/messages",
            json={"content": req.content},
            stream=True,
        )
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error communicating with sandbox: {exc}",
        ) from exc

    return StreamingResponse(
        response.iter_content(chunk_size=None),
        media_type="text/event-stream",
    )


@app.post("/workspaces/{workspace_id}/chats/{chat_id}/answer")
def answer_question(workspace_id: str, chat_id: str, req: AnswerRequest):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request(
            "POST",
            f"api/chats/{chat_id}/answer",
            json={"answers": req.answers},
        )
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.get("/workspaces/{workspace_id}/chats")
def list_chats(workspace_id: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("GET", "api/chats")
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.get("/workspaces/{workspace_id}/chats/{chat_id}")
def get_chat(workspace_id: str, chat_id: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("GET", f"api/chats/{chat_id}")
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.delete("/workspaces/{workspace_id}/chats/{chat_id}")
def delete_chat(workspace_id: str, chat_id: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("DELETE", f"api/chats/{chat_id}")
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.get("/workspaces/{workspace_id}/chats/{chat_id}/messages")
def get_messages(workspace_id: str, chat_id: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("GET", f"api/chats/{chat_id}/messages")
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.get("/workspaces/{workspace_id}/chats/{chat_id}/artifacts")
def list_chat_artifacts(workspace_id: str, chat_id: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        response = sandbox._request("GET", f"api/chats/{chat_id}/artifacts")
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    return response.json()


@app.get("/workspaces/{workspace_id}/files/download/{file_path:path}")
def download_workspace_file(workspace_id: str, file_path: str):
    sandbox = _get_sandbox_or_404(workspace_id)

    try:
        encoded_path = quote(file_path, safe="/")
        response = sandbox._request("GET", f"api/files/download/{encoded_path}", stream=True)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error communicating with sandbox: {exc}") from exc

    headers = {}
    for header in ("content-disposition", "content-length", "last-modified", "etag"):
        value = response.headers.get(header)
        if value:
            headers[header] = value

    return StreamingResponse(
        response.iter_content(chunk_size=None),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Snapshot endpoints
# ---------------------------------------------------------------------------
@app.post("/snapshots/triggers")
def create_snapshot_trigger(req: SnapshotTriggerRequest):
    sandbox = _get_sandbox_or_404(req.workspace_id)
    target_pod = sandbox.pod_name
    if not target_pod:
        raise HTTPException(
            status_code=400,
            detail="Workspace has no resolved pod_name yet",
        )

    trigger_name = f"{target_pod}-snapshot-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "podsnapshot.gke.io/v1alpha1",
        "kind": "PodSnapshotManualTrigger",
        "metadata": {
            "name": trigger_name,
            "namespace": SNAPSHOT_NAMESPACE,
        },
        "spec": {
            "targetPod": target_pod,
        },
    }

    api = _get_k8s_custom_api()
    try:
        created = api.create_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"trigger already exists: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=f"failed to create snapshot trigger: {detail}") from exc

    return {
        "name": created["metadata"]["name"],
        "namespace": created["metadata"]["namespace"],
        "target_pod": created.get("spec", {}).get("targetPod", target_pod),
        "api_version": created.get("apiVersion", "podsnapshot.gke.io/v1alpha1"),
        "kind": created.get("kind", "PodSnapshotManualTrigger"),
    }


@app.delete("/snapshots/triggers/{trigger_name}")
def delete_snapshot_trigger(trigger_name: str):
    api = _get_k8s_custom_api()
    try:
        api.delete_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Trigger not found: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {"deleted": True}


@app.get("/snapshots/status")
def get_snapshot_status(
    trigger_name: str,
):
    api = _get_k8s_custom_api()
    try:
        trigger = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            name=trigger_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Trigger not found: {trigger_name}") from exc
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    trigger_status = trigger.get("status", {})
    snapshot_created = trigger_status.get("snapshotCreated")
    snapshot_name = snapshot_created.get("name")
    if not snapshot_name:
        return {
            "ready": False,
            "snapshot_name": None,
        }

    try:
        snapshot = api.get_namespaced_custom_object(
            group="podsnapshot.gke.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="podsnapshots",
            name=snapshot_name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return {
                "ready": False,
                "snapshot_name": snapshot_name,
            }
        raise HTTPException(status_code=500, detail=exc.body or str(exc)) from exc

    return {
        "ready": _is_snapshot_ready(snapshot),
        "snapshot_name": snapshot_name,
    }


@app.post("/snapshots/restore")
def restore_from_snapshot(req: SnapshotRestoreRequest):
    api = _get_k8s_custom_api()
    _get_ready_snapshot_or_400(api, req.snapshot_name)

    claim_name = f"restore-{req.snapshot_name[:20]}-{uuid.uuid4().hex[:8]}"
    body = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": SNAPSHOT_NAMESPACE,
            "labels": {
                "app": "agent-sandbox-workload",
            },
            "annotations": {
                "podsnapshot.gke.io/ps-name": req.snapshot_name,
            },
        },
        "spec": {
            "sandboxTemplateRef": {
                "name": SANDBOX_TEMPLATE_NAME,
            },
        },
    }

    try:
        created = api.create_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1alpha1",
            namespace=SNAPSHOT_NAMESPACE,
            plural="sandboxclaims",
            body=body,
        )
    except ApiException as exc:
        detail = exc.body or str(exc)
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"sandboxclaim already exists: {claim_name}") from exc
        raise HTTPException(status_code=500, detail=f"failed to create sandboxclaim: {detail}") from exc

    sandbox = SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SNAPSHOT_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.claim_name = claim_name
    try:
        sandbox._wait_for_sandbox_ready()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"restore claim created but sandbox not ready: {exc}") from exc

    workspace_id = str(uuid.uuid4())
    workspaces[workspace_id] = sandbox

    return {"workspace_id": workspace_id}
