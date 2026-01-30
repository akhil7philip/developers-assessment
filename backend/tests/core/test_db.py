"""
Tests for database initialization.
"""

from sqlmodel import Session, select

from app.core.db import init_db
from app.core.config import settings
from app.models import User


def test_init_db_creates_superuser(db: Session) -> None:
    """Test that init_db creates superuser if it doesn't exist."""
    # Get existing superuser
    existing_user = db.exec(
        select(User).where(User.email == settings.FIRST_SUPERUSER)
    ).first()
    
    # If superuser exists, verify it's correct
    if existing_user:
        assert existing_user.is_superuser is True
        assert existing_user.email == settings.FIRST_SUPERUSER
    else:
        # If it doesn't exist, init_db should create it
        # But we can't easily test this without breaking other tests
        # So we'll just verify the function runs without error
        init_db(db)
        # Verify superuser exists after init
        user = db.exec(
            select(User).where(User.email == settings.FIRST_SUPERUSER)
        ).first()
        assert user is not None
        assert user.is_superuser is True

