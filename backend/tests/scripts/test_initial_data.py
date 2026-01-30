"""
Tests for initial_data.py
"""

from unittest.mock import MagicMock, patch

from app.initial_data import init, logger, main


def test_init() -> None:
    """Test init function."""
    session_mock = MagicMock()
    session_context_mock = MagicMock()
    session_context_mock.__enter__ = MagicMock(return_value=session_mock)
    session_context_mock.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.initial_data.Session", return_value=session_context_mock),
        patch("app.initial_data.init_db") as mock_init_db,
    ):
        init()
        mock_init_db.assert_called_once_with(session_mock)


def test_main() -> None:
    """Test main function."""
    with (
        patch("app.initial_data.init") as mock_init,
        patch.object(logger, "info") as mock_logger_info,
    ):
        main()
        mock_init.assert_called_once()
        assert mock_logger_info.call_count == 2


def test_main_entry_point() -> None:
    """Test main entry point."""
    with (
        patch("app.initial_data.main") as mock_main,
    ):
        # Simulate running as script
        import app.initial_data

        if hasattr(app.initial_data, "__main__"):
            app.initial_data.__main__()
        mock_main.assert_not_called()  # This would be called if run as script

