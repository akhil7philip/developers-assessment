"""
Tests for backend_pre_start.py exception handling.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.backend_pre_start import init, logger


def test_init_exception() -> None:
    """Test init function exception handling.

    Note: The init function has a @retry decorator that will retry up to max_tries times.
    To speed up the test, we'll access the underlying function and test it directly,
    or we can test that the exception is eventually raised after retries.
    """
    engine_mock = MagicMock()
    session_mock = MagicMock()
    exec_mock = MagicMock(side_effect=Exception("Database connection failed"))
    session_mock.exec = exec_mock

    session_context_mock = MagicMock()
    session_context_mock.__enter__ = MagicMock(return_value=session_mock)
    session_context_mock.__exit__ = MagicMock(return_value=False)

    # Access the underlying function from the retry wrapper
    # The retry decorator wraps the function, so we can access the original via __wrapped__
    try:
        original_init = init.__wrapped__
    except AttributeError:
        # If __wrapped__ doesn't exist, test the retry behavior directly
        # by ensuring exception is eventually raised
        with (
            patch("app.backend_pre_start.Session", return_value=session_context_mock),
            patch.object(logger, "error") as mock_logger_error,
            pytest.raises(Exception, match="Database connection failed"),
        ):
            # This will retry but eventually raise the exception
            init(engine_mock)
            assert mock_logger_error.called
    else:
        # Test the original function directly (without retry)
        with (
            patch("app.backend_pre_start.Session", return_value=session_context_mock),
            patch.object(logger, "error") as mock_logger_error,
            pytest.raises(Exception, match="Database connection failed"),
        ):
            original_init(engine_mock)
            mock_logger_error.assert_called_once()


def test_init_success() -> None:
    """Test init function successful execution."""
    engine_mock = MagicMock()
    session_mock = MagicMock()
    exec_mock = MagicMock(return_value=True)
    session_mock.exec = exec_mock

    session_context_mock = MagicMock()
    session_context_mock.__enter__ = MagicMock(return_value=session_mock)
    session_context_mock.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.backend_pre_start.Session", return_value=session_context_mock),
        patch.object(logger, "error") as mock_logger_error,
    ):
        init(engine_mock)
        # Should not log error on success
        mock_logger_error.assert_not_called()
        # Verify exec was called
        session_mock.exec.assert_called_once()

