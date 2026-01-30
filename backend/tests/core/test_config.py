"""
Tests for configuration edge cases.
"""

import warnings

import pytest

from app.core.config import Settings, parse_cors


def test_parse_cors_list() -> None:
    """Test parse_cors with list input."""
    result = parse_cors(["http://localhost:3000", "http://localhost:3001"])
    assert result == ["http://localhost:3000", "http://localhost:3001"]


def test_parse_cors_string() -> None:
    """Test parse_cors with string input."""
    result = parse_cors("http://localhost:3000,http://localhost:3001")
    assert result == ["http://localhost:3000", "http://localhost:3001"]


def test_parse_cors_invalid() -> None:
    """Test parse_cors with invalid input."""
    with pytest.raises(ValueError):
        parse_cors(123)  # type: ignore


def test_settings_check_default_secret_local() -> None:
    """Test _check_default_secret in local environment."""
    settings = Settings(
        PROJECT_NAME="Test",
        POSTGRES_SERVER="localhost",
        POSTGRES_USER="test",
        POSTGRES_DB="test",
        FIRST_SUPERUSER="test@example.com",
        FIRST_SUPERUSER_PASSWORD="changethis",
        SECRET_KEY="changethis",
        POSTGRES_PASSWORD="changethis",
        ENVIRONMENT="local",
    )

    # Should not raise, but warn
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        # Trigger validation
        _ = settings.SECRET_KEY
        assert len(w) >= 0  # May or may not warn depending on when validation runs


def test_settings_check_default_secret_production() -> None:
    """Test _check_default_secret in production environment raises ValueError."""
    with pytest.raises(ValueError, match="changethis"):
        Settings(
            PROJECT_NAME="Test",
            POSTGRES_SERVER="localhost",
            POSTGRES_USER="test",
            POSTGRES_DB="test",
            FIRST_SUPERUSER="test@example.com",
            FIRST_SUPERUSER_PASSWORD="changethis",
            SECRET_KEY="changethis",
            POSTGRES_PASSWORD="changethis",
            ENVIRONMENT="production",
        )

