"""
Tests for auth service edge cases.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import crud
from app.core.config import settings
from app.models import UserCreate
from app.utils import generate_password_reset_token
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string


def test_login_inactive_user(client: TestClient, db: Session) -> None:
    """Test login with inactive user."""
    email = random_email()
    password = random_lower_string()
    user_create = UserCreate(
        email=email,
        password=password,
        is_active=False,
        is_superuser=False,
    )
    crud.create_user(session=db, user_create=user_create)

    login_data = {
        "username": email,
        "password": password,
    }
    r = client.post(f"{settings.API_V1_STR}/login/access-token", data=login_data)
    assert r.status_code == 400
    assert "Inactive user" in r.json()["detail"]


def test_reset_password_user_not_found(client: TestClient) -> None:
    """Test reset password with non-existent user."""
    token = generate_password_reset_token(email="nonexistent@example.com")
    data = {"new_password": "newpassword", "token": token}
    r = client.post(
        f"{settings.API_V1_STR}/reset-password/",
        json=data,
    )
    assert r.status_code == 404
    assert "does not exist" in r.json()["detail"]


def test_reset_password_inactive_user(client: TestClient, db: Session) -> None:
    """Test reset password with inactive user."""
    email = random_email()
    password = random_lower_string()
    new_password = random_lower_string()

    user_create = UserCreate(
        email=email,
        password=password,
        is_active=False,
        is_superuser=False,
    )
    crud.create_user(session=db, user_create=user_create)

    token = generate_password_reset_token(email=email)
    data = {"new_password": new_password, "token": token}

    r = client.post(
        f"{settings.API_V1_STR}/reset-password/",
        json=data,
    )
    assert r.status_code == 400
    assert "Inactive user" in r.json()["detail"]


def test_recover_password_html_content(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Test password recovery HTML content endpoint."""
    email = random_email()
    password = random_lower_string()
    user_create = UserCreate(
        email=email,
        password=password,
        is_active=True,
        is_superuser=False,
    )
    crud.create_user(session=db, user_create=user_create)

    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "test@example.com"),
    ):
        r = client.post(
            f"{settings.API_V1_STR}/password-recovery-html-content/{email}",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


def test_recover_password_html_content_user_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Test password recovery HTML content with non-existent user."""
    email = "nonexistent@example.com"
    r = client.post(
        f"{settings.API_V1_STR}/password-recovery-html-content/{email}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404
    assert "does not exist" in r.json()["detail"]

