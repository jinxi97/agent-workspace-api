import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

from auth import AuthError, JWT_ALGORITHM, create_jwt, decode_jwt, verify_google_token

# Use a fixed secret for tests
TEST_SECRET = "test-secret-key"


@pytest.fixture(autouse=True)
def _set_jwt_secret(monkeypatch):
    monkeypatch.setattr("auth.JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr("auth.GOOGLE_CLIENT_ID", "test-client-id")


class TestVerifyGoogleToken:
    @patch("auth.google_id_token.verify_oauth2_token")
    def test_valid_token(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-user-123",
            "email": "user@example.com",
            "aud": "test-client-id",
        }

        result = verify_google_token("valid-token")

        assert result == {"sub": "google-user-123", "email": "user@example.com"}
        mock_verify.assert_called_once()

    @patch("auth.google_id_token.verify_oauth2_token")
    def test_invalid_token(self, mock_verify):
        mock_verify.side_effect = ValueError("Token expired")

        with pytest.raises(AuthError, match="Invalid Google token"):
            verify_google_token("bad-token")


class TestJWT:
    def test_create_and_decode_roundtrip(self):
        token = create_jwt("user-abc", "workspace-xyz")
        payload = decode_jwt(token)

        assert payload["sub"] == "user-abc"
        assert payload["workspace_id"] == "workspace-xyz"
        assert "iat" in payload
        assert "exp" in payload

    def test_decode_expired_token(self):
        payload = {
            "sub": "user-abc",
            "workspace_id": "workspace-xyz",
            "iat": time.time() - 7200,
            "exp": time.time() - 3600,
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm=JWT_ALGORITHM)

        with pytest.raises(AuthError, match="Token has expired"):
            decode_jwt(token)

    def test_decode_invalid_token(self):
        with pytest.raises(AuthError, match="Invalid token"):
            decode_jwt("not-a-real-token")

    def test_decode_wrong_secret(self):
        token = jwt.encode(
            {"sub": "user", "workspace_id": "ws"},
            "wrong-secret",
            algorithm=JWT_ALGORITHM,
        )

        with pytest.raises(AuthError, match="Invalid token"):
            decode_jwt(token)
