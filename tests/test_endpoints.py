import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from auth import create_jwt

# Use a fixed secret for tests
TEST_SECRET = "test-secret-key"
TEST_USER_ID = str(uuid.uuid4())
TEST_WORKSPACE_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _patch_auth_secret(monkeypatch):
    monkeypatch.setattr("auth.JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr("auth.GOOGLE_CLIENT_ID", "test-client-id")


@pytest.fixture()
def mock_db(monkeypatch):
    """Patch DB functions so we don't need a real Postgres for endpoint tests."""
    user = MagicMock()
    user.id = uuid.UUID(TEST_USER_ID)
    user.google_sub = "google-sub-test"
    user.email = "test@example.com"

    workspace = MagicMock()
    workspace.id = uuid.UUID(TEST_WORKSPACE_ID)
    workspace.user_id = uuid.UUID(TEST_USER_ID)
    workspace.claim_name = "sandbox-claim-test"
    workspace.template_name = "claude-agent-sandbox-template"

    monkeypatch.setattr("app.routers.auth.get_or_create_user", make_async_return(user))
    monkeypatch.setattr("app.routers.auth.get_workspace_by_user_id", make_async_return(workspace))
    monkeypatch.setattr("app.routers.auth.create_workspace_record", make_async_return(workspace))


def make_async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


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


class TestAuthGoogleEndpoint:
    @patch("app.routers.auth.verify_google_token")
    def test_new_user_creates_workspace(self, mock_verify, client, mock_db):
        mock_verify.return_value = {"sub": "google-sub-test", "email": "test@example.com"}

        # Patch get_workspace_by_user_id to return None (new user, no workspace)
        with patch("app.routers.auth.get_workspace_by_user_id", make_async_return(None)):
            mock_sandbox = MagicMock()
            mock_sandbox.claim_name = "sandbox-claim-new"
            with patch("app.routers.auth.SandboxClient", return_value=mock_sandbox):
                resp = client.post("/auth/google", json={"id_token": "valid-google-token"})

        assert resp.status_code == 200
        data = resp.json()
        assert "workspace_id" in data
        assert "token" in data

    @patch("app.routers.auth.verify_google_token")
    def test_existing_user_reuses_workspace(self, mock_verify, client, mock_db):
        mock_verify.return_value = {"sub": "google-sub-test", "email": "test@example.com"}

        resp = client.post("/auth/google", json={"id_token": "valid-google-token"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace_id"] == TEST_WORKSPACE_ID

    @patch("app.routers.auth.verify_google_token")
    def test_invalid_google_token_returns_401(self, mock_verify, client):
        from auth import AuthError
        mock_verify.side_effect = AuthError("Invalid Google token")

        resp = client.post("/auth/google", json={"id_token": "bad-token"})

        assert resp.status_code == 401
