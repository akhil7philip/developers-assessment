"""
Tests for utility functions.
"""

from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.utils import (
    generate_new_account_email,
    send_email,
)


def test_send_email_with_ssl() -> None:
    """Test send_email with SMTP_SSL enabled."""
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_PORT", 465),
        patch("app.core.config.settings.SMTP_SSL", True),
        patch("app.core.config.settings.SMTP_TLS", False),
        patch("app.core.config.settings.SMTP_USER", "user@example.com"),
        patch("app.core.config.settings.SMTP_PASSWORD", "password"),
        patch("app.core.config.settings.EMAILS_FROM_NAME", "Test"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "test@example.com"),
        patch("app.utils.emails.Message") as mock_message,
    ):
        mock_msg_instance = mock_message.return_value
        mock_msg_instance.send.return_value = True

        send_email(
            email_to="recipient@example.com",
            subject="Test",
            html_content="<html>Test</html>",
        )

        # Verify SSL was set
        call_args = mock_msg_instance.send.call_args
        smtp_options = call_args.kwargs.get("smtp", {})
        assert "ssl" in smtp_options
        assert smtp_options["ssl"] is True


def test_send_email_with_user_password() -> None:
    """Test send_email with SMTP_USER and SMTP_PASSWORD."""
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_PORT", 587),
        patch("app.core.config.settings.SMTP_TLS", True),
        patch("app.core.config.settings.SMTP_SSL", False),
        patch("app.core.config.settings.SMTP_USER", "user@example.com"),
        patch("app.core.config.settings.SMTP_PASSWORD", "password123"),
        patch("app.core.config.settings.EMAILS_FROM_NAME", "Test"),
        patch("app.core.config.settings.EMAILS_FROM_EMAIL", "test@example.com"),
        patch("app.utils.emails.Message") as mock_message,
    ):
        mock_msg_instance = mock_message.return_value
        mock_msg_instance.send.return_value = True

        send_email(
            email_to="recipient@example.com",
            subject="Test",
            html_content="<html>Test</html>",
        )

        # Verify user and password were set
        call_args = mock_msg_instance.send.call_args
        smtp_options = call_args.kwargs.get("smtp", {})
        assert smtp_options["user"] == "user@example.com"
        assert smtp_options["password"] == "password123"


def test_generate_new_account_email() -> None:
    """Test generate_new_account_email function."""
    with (
        patch("app.core.config.settings.PROJECT_NAME", "Test Project"),
        patch("app.core.config.settings.FRONTEND_HOST", "http://localhost:3000"),
        patch("app.utils.render_email_template") as mock_render,
    ):
        mock_render.return_value = "<html>New Account</html>"

        email_data = generate_new_account_email(
            email_to="newuser@example.com",
            username="newuser",
            password="password123",
        )

        assert email_data.subject == "Test Project - New account for user newuser"
        assert email_data.html_content == "<html>New Account</html>"
        mock_render.assert_called_once()
        call_kwargs = mock_render.call_args.kwargs
        assert call_kwargs["template_name"] == "new_account.html"
        assert call_kwargs["context"]["username"] == "newuser"
        assert call_kwargs["context"]["password"] == "password123"

