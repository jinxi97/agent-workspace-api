import os
import uuid

from fastapi import FastAPI, HTTPException
from agentic_sandbox import SandboxClient

app = FastAPI()

SANDBOX_TEMPLATE_NAME = os.getenv("SANDBOX_TEMPLATE_NAME", "python-runtime-template")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "pod-snapshots-ns")
SANDBOX_API_URL = os.getenv(
    "SANDBOX_API_URL",
    "http://sandbox-router-svc.default.svc.cluster.local:8080",
)

# Local sandbox API URL for development
# Remember to run 'kubectl -n default port-forward svc/sandbox-router-svc 8080:8080' before running the application
LOCAL_SANDBOX_API_URL = "http://127.0.0.1:8080"

# Store active workspaces
workspaces: dict[str, SandboxClient] = {}

@app.get("/")
async def root():
    return {"message": "Hello World!"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/workspaces")
def create_workspace():
    # Generate a unique workspace ID
    workspace_id = str(uuid.uuid4())
    sandbox = SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    )
    sandbox.__enter__()  # Start the sandbox

    # Store reference to the sandbox
    workspaces[workspace_id] = sandbox

    return {"workspace_id": workspace_id}


@app.post("/workspaces/{workspace_id}/exec")
def exec_command(workspace_id: str, command: str):
    sandbox = workspaces.get(workspace_id)
    if not sandbox:
        raise HTTPException(404, "Workspace not found")

    result = sandbox.run(command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code
    }


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    sandbox = workspaces.pop(workspace_id, None)
    if sandbox:
        sandbox.__exit__(None, None, None)  # Cleanup
    return {"deleted": True}