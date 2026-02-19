import os
import shlex
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from agentic_sandbox import SandboxClient

app = FastAPI()

SANDBOX_TEMPLATE_NAME = os.getenv("SANDBOX_TEMPLATE_NAME", "python-runtime-template")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "pod-snapshots-ns")
SANDBOX_API_URL = os.getenv(
    "SANDBOX_API_URL",
    "http://sandbox-router-svc.default.svc.cluster.local:8080",
)

@app.get("/")
async def root():
    return {"message": "Hello World!!"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/echo")
async def echo(message: str):
    with SandboxClient(
        template_name=SANDBOX_TEMPLATE_NAME,
        namespace=SANDBOX_NAMESPACE,
        api_url=SANDBOX_API_URL,
    ) as sandbox:
        result = sandbox.run(f"echo {message}")
        return result.stdout
