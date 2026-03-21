import uuid

import pytest
from fastapi.testclient import TestClient

from utils.auth import create_jwt

# Use a fixed secret for tests
TEST_SECRET = "test-secret-key"
TEST_USER_ID = str(uuid.uuid4())
TEST_WORKSPACE_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _patch_auth_secret(monkeypatch):
    monkeypatch.setattr("utils.auth.JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr("utils.auth.GOOGLE_CLIENT_ID", "test-client-id")



@pytest.fixture()
def no_db_lifespan(monkeypatch):
    """Skip DB init/close in lifespan by setting DATABASE_URL to empty."""
    monkeypatch.setattr("app.config.DATABASE_URL", "")
    monkeypatch.setattr("app.main.DATABASE_URL", "")


@pytest.fixture()
def client(no_db_lifespan):
    from app.main import app
    with TestClient(app) as c:
        yield c


def _make_token(user_id=TEST_USER_ID, workspace_id=TEST_WORKSPACE_ID):
    return create_jwt(user_id, workspace_id)


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestHealthzNoAuth:
    def test_healthz_returns_200_without_token(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_root_returns_200_without_token(self, client):
        resp = client.get("/")
        assert resp.status_code == 200


class TestAuthMiddleware:
    def test_workspace_endpoint_without_token_returns_401(self, client):
        resp = client.get(f"/workspaces/{TEST_WORKSPACE_ID}/chats")
        assert resp.status_code == 401

    def test_workspace_endpoint_with_invalid_token_returns_401(self, client):
        resp = client.get(
            f"/workspaces/{TEST_WORKSPACE_ID}/chats",
            headers=_auth_header("bad-token"),
        )
        assert resp.status_code == 401

    def test_workspace_endpoint_wrong_workspace_returns_403(self, client):
        other_workspace = str(uuid.uuid4())
        token = _make_token(workspace_id=TEST_WORKSPACE_ID)
        resp = client.get(
            f"/workspaces/{other_workspace}/chats",
            headers=_auth_header(token),
        )
        assert resp.status_code == 403

    def test_workspace_endpoint_with_valid_token_passes_middleware(self, client):
        """Token is valid and workspace matches — middleware passes.
        We'll get 404 from get_sandbox_or_404 since there's no sandbox
        in the in-memory cache, but the auth middleware itself passed."""
        token = _make_token()
        resp = client.get(
            f"/workspaces/{TEST_WORKSPACE_ID}/chats",
            headers=_auth_header(token),
        )
        # 404 = sandbox not in cache, but that means auth middleware passed
        assert resp.status_code == 404
