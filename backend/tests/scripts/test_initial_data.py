"""
Tests for initial_data.py
"""

import runpy
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
    """Test main entry point.

    This verifies that when the module is run as a script (python -m app.initial_data),
    the main() function gets executed. Since runpy.run_module with run_name='__main__'
    actually executes the code, we need to mock the database dependencies.
    """
    import sys

    # First, set up mocks for the database dependencies
    with (
        patch("app.core.db.init_db") as mock_init_db,
        patch("app.core.db.engine"),
    ):
        # Remove the module from cache to get a fresh load
        for module_name in list(sys.modules.keys()):
            if module_name == "app.initial_data" or module_name.startswith(
                "app.initial_data."
            ):
                del sys.modules[module_name]

        # Run the module as if it were the main script
        runpy.run_module("app.initial_data", run_name="__main__")

        # Verify init_db was called (main -> init -> init_db)
        mock_init_db.assert_called_once()
