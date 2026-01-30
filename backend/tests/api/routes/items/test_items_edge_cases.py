"""
Tests for items service edge cases - non-superuser path.
"""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import crud
from app.core.config import settings
from app.models import ItemCreate, UserCreate
from tests.utils.utils import random_email, random_lower_string


def test_get_items_non_superuser(
    client: TestClient, normal_user_token_headers: dict[str, str], db: Session
) -> None:
    """Test get_items for non-superuser (should only see own items)."""
    from sqlmodel import select

    from app.models import User

    # Get the normal user
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).first()
    assert user is not None

    item_in = ItemCreate(title="My Item", description="My description")
    crud.create_item(session=db, item_in=item_in, owner_id=user.id)

    # Create another user and item
    other_email = random_email()
    other_user_create = UserCreate(
        email=other_email,
        password=random_lower_string(),
        is_active=True,
        is_superuser=False,
    )
    other_user = crud.create_user(session=db, user_create=other_user_create)
    other_item_in = ItemCreate(title="Other Item", description="Other description")
    crud.create_item(session=db, item_in=other_item_in, owner_id=other_user.id)

    # Get items as normal user
    r = client.get(
        f"{settings.API_V1_STR}/items/",
        headers=normal_user_token_headers,
    )
    assert r.status_code == 200
    data = r.json()
    # Should only see own items
    assert all(item["owner_id"] == str(user.id) for item in data["data"])
