"""
Tests for users service edge cases.
"""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.conftest import superuser_token_headers


def test_get_user_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Test get_user with non-existent user."""
    import uuid

    fake_id = uuid.uuid4()
    r = client.get(
        f"{settings.API_V1_STR}/users/{fake_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404
    assert "User not found" in r.json()["detail"]

