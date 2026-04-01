import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from utils.auth import create_jwt

TEST_SECRET = "test-secret-key"
TEST_USER_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _patch_auth_secret(monkeypatch):
    monkeypatch.setattr("utils.auth.JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr("utils.auth.GOOGLE_CLIENT_ID", "test-client-id")


@pytest.fixture()
def no_db_lifespan(monkeypatch):
    monkeypatch.setattr("app.config.CLOUD_SQL_CONNECTION_NAME", "")


@pytest.fixture()
def client(no_db_lifespan):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_token(user_id=TEST_USER_ID):
    return create_jwt(user_id)


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


def _make_user(user_id=TEST_USER_ID):
    user = MagicMock()
    user.id = uuid.UUID(user_id)
    user.email = "test@example.com"
    return user


def _make_api_key(user_id=TEST_USER_ID, name="my-key", masked="sk-...abcd"):
    key = MagicMock()
    key.id = uuid.uuid4()
    key.user_id = uuid.UUID(user_id)
    key.key_masked = masked
    key.name = name
    key.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return key


# ---------------------------------------------------------------------------
# POST /account
# ---------------------------------------------------------------------------
class TestCreateOrGetAccount:
    @patch("app.routers.account.verify_google_token")
    @patch("app.routers.account.get_or_create_user")
    def test_valid_google_token_returns_jwt(self, mock_get_user, mock_verify, client):
        user = _make_user()
        mock_verify.return_value = {"sub": "google-123", "email": "test@example.com"}
        mock_get_user.side_effect = _async_return(user)

        resp = client.post("/account", json={"id_token": "valid-token"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == TEST_USER_ID
        assert "token" in data
        mock_get_user.assert_called_once_with("google", "google-123", "test@example.com")

    @patch("app.routers.account.verify_google_token")
    def test_invalid_google_token_returns_401(self, mock_verify, client):
        from utils.auth import AuthError

        mock_verify.side_effect = AuthError("Invalid Google token")

        resp = client.post("/account", json={"id_token": "bad-token"})

        assert resp.status_code == 401
        assert "Invalid Google token" in resp.json()["detail"]

    def test_missing_id_token_returns_422(self, client):
        resp = client.post("/account", json={})
        assert resp.status_code == 422

    def test_empty_id_token_returns_422(self, client):
        resp = client.post("/account", json={"id_token": ""})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /account/api-keys
# ---------------------------------------------------------------------------
class TestCreateApiKey:
    def test_without_token_returns_401(self, client):
        resp = client.post("/account/api-keys", json={"name": "test-key"})
        assert resp.status_code == 401

    def test_with_invalid_token_returns_401(self, client):
        resp = client.post(
            "/account/api-keys",
            json={"name": "test-key"},
            headers=_auth_header("bad-token"),
        )
        assert resp.status_code == 401

    def test_creates_key_and_returns_raw(self, client, monkeypatch):
        api_key = _make_api_key()
        raw_key = "sk-abc123secret"
        monkeypatch.setattr(
            "app.routers.account.create_api_key",
            _async_return((api_key, raw_key)),
        )

        resp = client.post(
            "/account/api-keys",
            json={"name": "my-key"},
            headers=_auth_header(_make_token()),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["key"] == raw_key
        assert data["id"] == str(api_key.id)
        assert "created_at" in data


# ---------------------------------------------------------------------------
# GET /account/api-keys
# ---------------------------------------------------------------------------
class TestListApiKeys:
    def test_without_token_returns_401(self, client):
        resp = client.get("/account/api-keys")
        assert resp.status_code == 401

    @patch("app.routers.account.list_api_keys")
    def test_returns_masked_keys(self, mock_list, client):
        keys = [
            _make_api_key(name="key-1", masked="sk-...aaaa"),
            _make_api_key(name="key-2", masked="sk-...bbbb"),
        ]
        mock_list.side_effect = _async_return(keys)

        resp = client.get(
            "/account/api-keys",
            headers=_auth_header(_make_token()),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["key_masked"] == "sk-...aaaa"
        assert data[0]["name"] == "key-1"
        assert data[1]["key_masked"] == "sk-...bbbb"
        # Raw key should never appear
        assert "key" not in data[0]

    @patch("app.routers.account.list_api_keys")
    def test_returns_empty_list(self, mock_list, client):
        mock_list.side_effect = _async_return([])

        resp = client.get(
            "/account/api-keys",
            headers=_auth_header(_make_token()),
        )

        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# DELETE /account/api-keys/{api_key_id}
# ---------------------------------------------------------------------------
class TestDeleteApiKey:
    def test_without_token_returns_401(self, client):
        resp = client.delete(f"/account/api-keys/{uuid.uuid4()}")
        assert resp.status_code == 401

    @patch("app.routers.account.delete_api_key")
    def test_deletes_existing_key(self, mock_delete, client):
        key_id = uuid.uuid4()
        mock_delete.side_effect = _async_return(True)

        resp = client.delete(
            f"/account/api-keys/{key_id}",
            headers=_auth_header(_make_token()),
        )

        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        mock_delete.assert_called_once_with(key_id, uuid.UUID(TEST_USER_ID))

    @patch("app.routers.account.delete_api_key")
    def test_nonexistent_key_returns_404(self, mock_delete, client):
        mock_delete.side_effect = _async_return(False)

        resp = client.delete(
            f"/account/api-keys/{uuid.uuid4()}",
            headers=_auth_header(_make_token()),
        )

        assert resp.status_code == 404
        assert "API key not found" in resp.json()["detail"]
