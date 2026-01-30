"""
Tests for API dependencies.
"""

import uuid
from datetime import timedelta

import jwt

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import crud
from app.core.config import settings
from app.core.security import create_access_token
from app.models import UserCreate
from tests.utils.utils import random_email, random_lower_string


def test_get_current_user_invalid_token(client: TestClient) -> None:
    """Test get_current_user with invalid token."""
    headers = {"Authorization": "Bearer invalid_token"}
    r = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
    )
    assert r.status_code == 403
    assert "Could not validate credentials" in r.json()["detail"]


def test_get_current_user_not_found(client: TestClient, db: Session) -> None:
    """Test get_current_user with non-existent user."""
    # Create a token for a non-existent user (use a valid UUID format)
    fake_user_id = uuid.uuid4()
    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(str(fake_user_id), expires_delta=expires_delta)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
    )
    assert r.status_code == 404
    assert "User not found" in r.json()["detail"]


def test_get_current_user_inactive(client: TestClient, db: Session) -> None:
    """Test get_current_user with inactive user."""
    email = random_email()
    password = random_lower_string()
    user_create = UserCreate(
        email=email,
        password=password,
        is_active=False,
        is_superuser=False,
    )
    user = crud.create_user(session=db, user_create=user_create)
    # Ensure user is committed and refreshed
    db.refresh(user)

    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(str(user.id), expires_delta=expires_delta)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
    )
    assert r.status_code == 400
    assert "Inactive user" in r.json()["detail"]


def test_get_current_user_invalid_token_payload(client: TestClient) -> None:
    """Test get_current_user with invalid token payload."""
    # Create a token with invalid payload
    invalid_payload = {"invalid": "data"}
    token = jwt.encode(
        invalid_payload,
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
    )
    assert r.status_code == 403
    assert "Could not validate credentials" in r.json()["detail"]

