"""
Tests for model properties and methods.
"""

from datetime import date
from decimal import Decimal

from app.models import TimeSegment


def test_time_segment_gross_amount() -> None:
    """Test TimeSegment gross_amount property."""
    segment = TimeSegment(
        hours_worked=Decimal("10.5"),
        hourly_rate=Decimal("25.00"),
        segment_date=date(2024, 1, 1),
    )
    assert segment.gross_amount == Decimal("262.50")

