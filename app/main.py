from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CLOUD_SQL_CONNECTION_NAME, DATABASE_URL
from app.middleware import require_auth
from app.routers import account, snapshots, workspaces, workspaces_with_agent
from utils.db import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.middleware("http")(require_auth)

app.include_router(account.router)
app.include_router(workspaces.router)
app.include_router(workspaces_with_agent.router)
app.include_router(snapshots.router)


@app.get("/")
async def root():
    return {"message": "Hello World!"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
