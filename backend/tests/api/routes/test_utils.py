"""
Tests for utils endpoints.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.conftest import superuser_token_headers


def test_health_check(client: TestClient) -> None:
    """Test health check endpoint."""
    r = client.get(f"{settings.API_V1_STR}/utils/health-check/")
    assert r.status_code == 200
    assert r.json() is True


def test_test_email(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Test email sending endpoint."""
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "test@example.com"),
        patch("app.api.routes.utils.service.send_email") as mock_send_email,
    ):
        email = "test@example.com"
        # For POST requests, FastAPI treats simple types as query parameters by default
        # So we send it as a query parameter
        r = client.post(
            f"{settings.API_V1_STR}/utils/test-email/",
            headers=superuser_token_headers,
            params={"email_to": email},
        )
        assert r.status_code == 201
        assert r.json() == {"message": "Test email sent"}
        mock_send_email.assert_called_once()

