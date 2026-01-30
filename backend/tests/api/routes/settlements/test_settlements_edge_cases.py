"""
Edge Cases and Exception Handling Tests for WorkLog Settlement System.

This file covers:
- Critical edge cases and multi-worker scenarios
- Failed settlement handling
- Pagination edge cases
- Cross-period work
- Data integrity tests
- Exception handling
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.routes.settlements.service import SettlementService, WorkLogService
from app.core.db import engine
from app.main import app
from app.models import (
    Adjustment,
    AdjustmentType,
    Remittance,
    RemittanceLine,
    RemittanceStatus,
    Settlement,
    TimeSegment,
    User,
    WorkLog,
    WorkLogRemittanceFilter,
)

client = TestClient(app)


def _cleanup_all_data(session: Session) -> None:
    """Clean up all test data."""
    session.exec(delete(RemittanceLine))
    session.exec(delete(Remittance))
    session.exec(delete(Settlement))
    session.exec(delete(Adjustment))
    session.exec(delete(TimeSegment))
    session.exec(delete(WorkLog))
    session.commit()


@pytest.fixture
def clean_session():
    """Create a clean database session."""
    with Session(engine) as session:
        _cleanup_all_data(session)
        yield session
        _cleanup_all_data(session)


@pytest.fixture
def test_user(clean_session: Session) -> User:
    """Create a test user."""
    import uuid as uuid_lib

    user = User(
        email=f"test_{uuid_lib.uuid4()}@example.com",
        hashed_password="dummy",
        is_active=True,
        is_superuser=False,
    )
    clean_session.add(user)
    clean_session.commit()
    clean_session.refresh(user)
    return user


# Critical Scenarios


def test_new_work_after_settlement(clean_session: Session, test_user: User):
    """
    CRITICAL: Test that new time segments added after payment are handled correctly.

    Requirement: "Additional time segments may be recorded against previously settled work"

    Scenario:
    1. WorkLog has 2 segments, both paid
    2. New segment added to same worklog
    3. Next settlement should only pay the new segment
    """
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-NEW-WORK",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Add initial segments
    segment1 = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    segment2 = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today() + timedelta(days=1),
    )
    clean_session.add(segment1)
    clean_session.add(segment2)
    clean_session.commit()

    # First settlement - pays $400 (8 hours * $50)
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today() + timedelta(days=1)
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    assert remittance1.net_amount == Decimal("400.00")

    # Mark as PAID
    remittance1.status = RemittanceStatus.PAID
    remittance1.paid_at = datetime.utcnow()
    clean_session.add(remittance1)
    clean_session.commit()

    # Add NEW segment to previously settled worklog
    segment3 = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("4.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today() + timedelta(days=2),
    )
    clean_session.add(segment3)
    clean_session.commit()

    # Second settlement - should only pay NEW work ($200)
    settlement2 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today() + timedelta(days=2)
    )

    remittance2 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).first()

    assert remittance2 is not None
    assert remittance2.net_amount == Decimal("200.00")  # Only new 4 hours

    # Verify lines - should only have 1 line for the new segment
    lines = clean_session.exec(
        select(RemittanceLine).where(RemittanceLine.remittance_id == remittance2.id)
    ).all()

    assert len(lines) == 1
    assert lines[0].time_segment_id == segment3.id


def test_multiple_workers_single_settlement(clean_session: Session):
    """
    Test that settlement correctly handles multiple workers.

    Scenario: 3 workers with different amounts in same period
    """
    # Create 3 workers
    workers = []
    for i in range(3):
        user = User(
            email=f"worker{i}@example.com",
            hashed_password="dummy",
            is_active=True,
            is_superuser=False,
        )
        clean_session.add(user)
        workers.append(user)
    clean_session.commit()

    # Add work for each worker
    expected_amounts = [Decimal("300.00"), Decimal("500.00"), Decimal("750.00")]

    for i, (worker, expected_amount) in enumerate(
        zip(workers, expected_amounts, strict=True)
    ):
        worklog = WorkLog(
            worker_user_id=worker.id,
            task_identifier=f"TASK-{i}",
        )
        clean_session.add(worklog)
        clean_session.flush()

        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=expected_amount / Decimal("50.00"),  # $50/hour
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_session.add(segment)
    clean_session.commit()

    # Run settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    # Verify settlement summary
    assert settlement.total_remittances_generated == 3

    # Verify each worker got correct amount
    remittances = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).all()

    assert len(remittances) == 3

    actual_amounts = sorted([r.net_amount for r in remittances])
    expected_sorted = sorted(expected_amounts)

    assert actual_amounts == expected_sorted


def test_negative_net_amount(clean_session: Session, test_user: User):
    """
    Test handling of negative net amounts (deductions > gross).

    Business Question: Can a worker owe money?
    Current implementation: Creates remittance with negative amount
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-NEGATIVE",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Add work worth $100
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("2.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    # Add deduction of $150
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("150.00"),
        reason="Penalty exceeds work value",
    )
    clean_session.add(adjustment)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    assert remittance.gross_amount == Decimal("100.00")
    assert remittance.adjustments_amount == Decimal("-150.00")
    assert remittance.net_amount == Decimal("-50.00")  # Worker owes $50


def test_zero_net_amount_not_counted(clean_session: Session, test_user: User):
    """
    Test that zero net amount remittances are not counted.

    Per code line 146: if net_amount != Decimal("0"), count it
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-ZERO",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Add work worth $200
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("4.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    # Add deduction of exactly $200
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("200.00"),
        reason="Exactly cancels out work",
    )
    clean_session.add(adjustment)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    # Should NOT count in total
    assert settlement.total_remittances_generated == 0

    # But remittance should still exist
    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    assert remittance.net_amount == Decimal("0.00")


def test_cancelled_remittance_reconciliation(clean_session: Session, test_user: User):
    """
    Test that CANCELLED remittances are reconciled like FAILED ones.
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-CANCELLED",
    )
    clean_session.add(worklog)
    clean_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("60.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)
    clean_session.commit()

    # First settlement
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    # Mark as CANCELLED
    remittance1.status = RemittanceStatus.CANCELLED
    clean_session.add(remittance1)
    clean_session.commit()

    # Second settlement - should reconcile cancelled payment
    # NOTE: Current implementation only checks for FAILED, not CANCELLED
    # This test will FAIL if CANCELLED is not handled
    settlement2 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance2 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).first()

    # Should create new remittance (or this test identifies a bug)
    assert remittance2 is not None
    assert remittance2.net_amount == Decimal("600.00")


def test_partially_remitted_worklog(clean_session: Session, test_user: User):
    """
    Test worklog with some segments paid and some not.

    Expected: is_remitted should be False (not fully paid)
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-PARTIAL",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Add 3 segments
    segments = []
    for i in range(3):
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today() + timedelta(days=i),
        )
        clean_session.add(segment)
        segments.append(segment)
    clean_session.commit()

    # First settlement - pays first 2 segments
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today() + timedelta(days=1)
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    remittance1.status = RemittanceStatus.PAID
    remittance1.paid_at = datetime.utcnow()
    clean_session.add(remittance1)
    clean_session.commit()

    # Check remittance status - should be FALSE (third segment not paid)
    is_remitted = WorkLogService._is_worklog_remitted(clean_session, worklog.id)
    assert not is_remitted

    # Total amount should include ALL segments
    total_amount = WorkLogService._calculate_worklog_amount(clean_session, worklog.id)
    assert total_amount == Decimal("750.00")  # 3 * 5 * 50


def test_filter_unremitted_worklogs(clean_session: Session, test_user: User):
    """
    Test filtering by UNREMITTED status.

    Current coverage: Only REMITTED filter tested
    """
    # Create 2 worklogs
    worklog_unpaid = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="UNPAID-WORK",
    )
    worklog_paid = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="PAID-WORK",
    )
    clean_session.add(worklog_unpaid)
    clean_session.add(worklog_paid)
    clean_session.flush()

    # Add segment to paid worklog only (unpaid worklog has no segments yet)
    segment_paid = TimeSegment(
        worklog_id=worklog_paid.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment_paid)
    clean_session.commit()

    # Pay the paid worklog
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittances = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).all()

    # Mark remittance as PAID
    for remittance in remittances:
        remittance.status = RemittanceStatus.PAID
        remittance.paid_at = datetime.utcnow()
        clean_session.add(remittance)
    clean_session.commit()

    # Now add segment to unpaid worklog (after settlement)
    segment_unpaid = TimeSegment(
        worklog_id=worklog_unpaid.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today()
        + timedelta(days=1),  # Different date to avoid being in same settlement
    )
    clean_session.add(segment_unpaid)
    clean_session.commit()

    # Filter by UNREMITTED
    result = WorkLogService.list_all_worklogs(
        clean_session,
        remittance_filter=WorkLogRemittanceFilter.UNREMITTED,
        skip=0,
        limit=100,
    )

    # Should only include unpaid worklog
    assert result.count >= 1
    identifiers = [wl.task_identifier for wl in result.data]
    assert "UNPAID-WORK" in identifiers
    assert "PAID-WORK" not in identifiers


def test_mixed_adjustment_types(clean_session: Session, test_user: User):
    """
    Test worklog with both ADDITION and DEDUCTION adjustments.
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-MIXED",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Add work
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    # Add bonus
    bonus = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("100.00"),
        reason="Performance bonus",
    )
    clean_session.add(bonus)

    # Add penalty
    penalty = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Quality issue",
    )
    clean_session.add(penalty)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance.gross_amount == Decimal("500.00")
    assert remittance.adjustments_amount == Decimal("50.00")  # +100 - 50
    assert remittance.net_amount == Decimal("550.00")

    # Verify 2 adjustment lines created
    adj_lines = clean_session.exec(
        select(RemittanceLine).where(
            RemittanceLine.remittance_id == remittance.id,
            RemittanceLine.adjustment_id.is_not(None),
        )
    ).all()

    assert len(adj_lines) == 2


def test_multiple_worklogs_per_worker(clean_session: Session, test_user: User):
    """
    Test worker with multiple worklogs (different tasks) in same period.

    Expected: Single remittance aggregating all work
    """
    # Create 3 worklogs for same worker
    worklogs = []
    expected_total = Decimal("0.00")

    for i in range(3):
        worklog = WorkLog(
            worker_user_id=test_user.id,
            task_identifier=f"TASK-{i}",
        )
        clean_session.add(worklog)
        clean_session.flush()

        hours = Decimal(str(5 + i * 2))  # 5, 7, 9 hours
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=hours,
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_session.add(segment)
        expected_total += hours * Decimal("50.00")
        worklogs.append(worklog)

    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    # Should create SINGLE remittance for this worker
    remittances = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).all()

    assert len(remittances) == 1
    assert remittances[0].net_amount == expected_total  # Total of all worklogs


def test_failed_settlement_with_adjustments_not_double_applied(
    clean_session: Session, test_user: User
):
    """
    CRITICAL BUG TEST: Verify adjustments from failed settlements not double-applied.

    Current Risk: _find_applicable_adjustments only checks PAID, not FAILED
    If adjustment was in FAILED remittance, might be applied again!
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-FAILED-ADJ",
    )
    clean_session.add(worklog)
    clean_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("100.00"),
        reason="One-time penalty",
    )
    clean_session.add(adjustment)
    clean_session.commit()

    # First settlement
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    # Should be $400 (500 - 100)
    assert remittance1.net_amount == Decimal("400.00")

    # Mark as FAILED
    remittance1.status = RemittanceStatus.FAILED
    clean_session.add(remittance1)
    clean_session.commit()

    # Second settlement - reconciles failed one
    settlement2 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance2 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).first()

    # Should STILL be $400, NOT $300 (adjustment applied twice)
    assert remittance2.net_amount == Decimal("400.00")

    # Verify adjustment only appears once in remittance lines across both remittances
    all_adj_lines = clean_session.exec(
        select(RemittanceLine).where(RemittanceLine.adjustment_id == adjustment.id)
    ).all()

    # Should have 2 lines (one in each remittance), but same adjustment ID
    # This is actually CORRECT - the adjustment should be included in both attempts
    # The key is the AMOUNT should be the same, not doubled
    assert len(all_adj_lines) == 2  # Once in failed, once in retry
    assert all_adj_lines[0].amount == all_adj_lines[1].amount  # Same amount


# Missing Scenarios


def test_cross_period_work_segments_outside_period(
    clean_session: Session, test_user: User
):
    """
    Test that segments outside the settlement period are excluded.

    Scenario:
    - Settlement for Jan 1-15
    - WorkLog has segments on Jan 10 (included) and Jan 20 (excluded)
    - Only Jan 10 segment should be included
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-CROSS-PERIOD",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Segment within period (Jan 10)
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 15)

    segment_in_period = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2026, 1, 10),  # Within period
    )
    clean_session.add(segment_in_period)

    # Segment outside period (Jan 20)
    segment_outside = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2026, 1, 20),  # Outside period
    )
    clean_session.add(segment_outside)
    clean_session.commit()

    # Generate settlement for Jan 1-15
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, period_start, period_end
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    # Should only include segment_in_period: 5 * 50 = 250
    assert remittance.gross_amount == Decimal("250.00")
    assert remittance.net_amount == Decimal("250.00")

    # Verify remittance lines - should only have 1 line
    lines = clean_session.exec(
        select(RemittanceLine).where(RemittanceLine.remittance_id == remittance.id)
    ).all()

    assert len(lines) == 1
    assert lines[0].time_segment_id == segment_in_period.id
    assert lines[0].time_segment_id != segment_outside.id


def test_date_boundary_conditions_inclusive(clean_session: Session, test_user: User):
    """
    Test that date boundaries are inclusive (period_start and period_end both included).

    Scenario:
    - Settlement for Jan 1-15
    - Segments on Jan 1 (period_start) and Jan 15 (period_end) should both be included
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-BOUNDARIES",
    )
    clean_session.add(worklog)
    clean_session.flush()

    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 15)

    # Segment on period_start
    segment_start = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("4.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=period_start,  # Exactly on start boundary
    )
    clean_session.add(segment_start)

    # Segment on period_end
    segment_end = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("6.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=period_end,  # Exactly on end boundary
    )
    clean_session.add(segment_end)

    # Segment just before period_start (should be excluded)
    segment_before = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("2.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2025, 12, 31),  # One day before
    )
    clean_session.add(segment_before)

    # Segment just after period_end (should be excluded)
    segment_after = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2026, 1, 16),  # One day after
    )
    clean_session.add(segment_after)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, period_start, period_end
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    # Should include segment_start (200) + segment_end (300) = 500
    assert remittance.gross_amount == Decimal("500.00")

    # Verify lines - should have 2 lines (start and end boundaries)
    lines = clean_session.exec(
        select(RemittanceLine).where(RemittanceLine.remittance_id == remittance.id)
    ).all()

    assert len(lines) == 2
    segment_ids = {lines[0].time_segment_id, lines[1].time_segment_id}
    assert segment_start.id in segment_ids
    assert segment_end.id in segment_ids
    assert segment_before.id not in segment_ids
    assert segment_after.id not in segment_ids


def test_pagination_edge_case_skip_beyond_count(
    clean_session: Session, test_user: User
):
    """
    Test pagination when skip exceeds total count.

    Expected: Should return empty list but count should still be correct
    """
    # Create 3 worklogs
    for i in range(3):
        worklog = WorkLog(
            worker_user_id=test_user.id,
            task_identifier=f"PAGINATE-{i}",
        )
        clean_session.add(worklog)
        clean_session.flush()

        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("1.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_session.add(segment)
    clean_session.commit()

    # Skip beyond total count
    result = WorkLogService.list_all_worklogs(
        clean_session, remittance_filter=None, skip=10, limit=100
    )

    # Should return empty data but correct count
    assert len(result.data) == 0
    assert result.count >= 3  # Total count should still be correct


def test_pagination_count_is_total_filtered(clean_session: Session, test_user: User):
    """
    Test that count reflects total filtered results, not just current page.

    Scenario:
    - 5 worklogs total (3 remitted, 2 not remitted)
    - Filter by REMITTED with limit=2
    - Count should be 3 (total filtered), not 2 (current page)

    Note: Since all worklogs for same worker are aggregated, we create separate workers
    to test partial remittance scenario.
    """
    import uuid as uuid_lib

    # Create 5 worklogs with 3 different workers (to get 3 separate remittances)
    workers = []
    for i in range(3):
        user = User(
            email=f"worker_{i}_{uuid_lib.uuid4()}@example.com",
            hashed_password="dummy",
            is_active=True,
            is_superuser=False,
        )
        clean_session.add(user)
        workers.append(user)
    clean_session.commit()

    worklogs = []
    # Create 3 worklogs for first 3 workers (will be remitted)
    for i in range(3):
        worklog = WorkLog(
            worker_user_id=workers[i].id,
            task_identifier=f"COUNT-TEST-REMITTED-{i}",
        )
        clean_session.add(worklog)
        clean_session.flush()

        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("1.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_session.add(segment)
        worklogs.append(worklog)

    # Create 2 worklogs for test_user (will NOT be remitted)
    for i in range(2):
        worklog = WorkLog(
            worker_user_id=test_user.id,
            task_identifier=f"COUNT-TEST-UNREMITTED-{i}",
        )
        clean_session.add(worklog)
        clean_session.flush()

        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("1.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_session.add(segment)
        worklogs.append(worklog)
    clean_session.commit()

    # Generate settlement - creates 4 remittances (3 for workers, 1 for test_user with 2 worklogs)
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittances = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).all()

    # Find remittances for the 3 workers (not test_user)
    worker_remittances = [
        r for r in remittances if r.worker_user_id in [w.id for w in workers]
    ]

    # Mark worker remittances as PAID (makes 3 worklogs remitted)
    for remittance in worker_remittances:
        remittance.status = RemittanceStatus.PAID
        remittance.paid_at = datetime.utcnow()
        clean_session.add(remittance)
    clean_session.commit()

    # Filter by REMITTED with limit=2
    result = WorkLogService.list_all_worklogs(
        clean_session,
        remittance_filter=WorkLogRemittanceFilter.REMITTED,
        skip=0,
        limit=2,
    )

    # Should return 2 items (limit)
    assert len(result.data) == 2
    # But count should be 3 (total filtered - 3 remitted worklogs from 3 workers)
    assert result.count == 3

    # Verify test_user's worklogs are NOT remitted
    unremitted_result = WorkLogService.list_all_worklogs(
        clean_session,
        remittance_filter=WorkLogRemittanceFilter.UNREMITTED,
        skip=0,
        limit=100,
    )
    unremitted_identifiers = [wl.task_identifier for wl in unremitted_result.data]
    assert "COUNT-TEST-UNREMITTED-0" in unremitted_identifiers
    assert "COUNT-TEST-UNREMITTED-1" in unremitted_identifiers


def test_concurrent_settlements_same_period_idempotent(
    clean_session: Session, test_user: User
):
    """
    Test that settling the same period twice is idempotent.

    Expected: Second settlement should not create duplicate remittances for already-paid work
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-CONCURRENT",
    )
    clean_session.add(worklog)
    clean_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("8.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)
    clean_session.commit()

    period_start = date.today()
    period_end = date.today()

    # First settlement
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, period_start, period_end
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    assert remittance1.net_amount == Decimal("400.00")

    # Mark as PAID
    remittance1.status = RemittanceStatus.PAID
    remittance1.paid_at = datetime.utcnow()
    clean_session.add(remittance1)
    clean_session.commit()

    # Second settlement for same period - should be idempotent
    settlement2 = SettlementService.generate_remittances_for_period(
        clean_session, period_start, period_end
    )

    # Should create settlement but no new remittances (work already paid)
    remittances2 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).all()

    # Should have 0 remittances (or remittance with $0 net_amount)
    total_new_payment = sum(r.net_amount for r in remittances2)
    assert total_new_payment == Decimal("0.00")
    assert settlement2.total_remittances_generated == 0


def test_worker_without_worklogs_not_in_settlement(clean_session: Session):
    """
    Test that workers without any worklogs don't appear in settlements.

    Scenario:
    - User exists but has no WorkLogs
    - Settlement should not create remittance for this user
    """
    # Create user without any worklogs
    user = User(
        email="no_worklogs@example.com",
        hashed_password="dummy",
        is_active=True,
        is_superuser=False,
    )
    clean_session.add(user)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    # Should not create any remittances
    remittances = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).all()

    # Verify no remittances created for user without worklogs
    user_remittances = [r for r in remittances if r.worker_user_id == user.id]
    assert len(user_remittances) == 0


def test_remittance_lines_integrity_all_segments_have_lines(
    clean_session: Session, test_user: User
):
    """
    Test that every time segment in a remittance has a corresponding RemittanceLine.

    This verifies data integrity - all segments should be tracked in remittance lines
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-INTEGRITY-SEGMENTS",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Create 3 segments
    segments = []
    for i in range(3):
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today() + timedelta(days=i),
        )
        clean_session.add(segment)
        segments.append(segment)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today() + timedelta(days=2)
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None

    # Get all remittance lines for time segments
    lines = clean_session.exec(
        select(RemittanceLine).where(
            RemittanceLine.remittance_id == remittance.id,
            RemittanceLine.time_segment_id.is_not(None),
        )
    ).all()

    # Should have 3 lines (one per segment)
    assert len(lines) == 3

    # Verify each segment has a corresponding line
    segment_ids_in_lines = {line.time_segment_id for line in lines}
    segment_ids = {segment.id for segment in segments}

    assert segment_ids_in_lines == segment_ids

    # Verify line amounts match segment amounts
    for line in lines:
        segment = next(s for s in segments if s.id == line.time_segment_id)
        expected_amount = segment.hours_worked * segment.hourly_rate
        assert line.amount == expected_amount


def test_remittance_lines_integrity_all_adjustments_have_lines(
    clean_session: Session, test_user: User
):
    """
    Test that every adjustment in a remittance has a corresponding RemittanceLine.

    This verifies data integrity - all adjustments should be tracked in remittance lines
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-INTEGRITY-ADJUSTMENTS",
    )
    clean_session.add(worklog)
    clean_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    # Create 2 adjustments
    adjustments = []
    bonus = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("100.00"),
        reason="Bonus",
    )
    clean_session.add(bonus)
    adjustments.append(bonus)

    penalty = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Penalty",
    )
    clean_session.add(penalty)
    adjustments.append(penalty)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None

    # Get all remittance lines for adjustments
    adj_lines = clean_session.exec(
        select(RemittanceLine).where(
            RemittanceLine.remittance_id == remittance.id,
            RemittanceLine.adjustment_id.is_not(None),
        )
    ).all()

    # Should have 2 lines (one per adjustment)
    assert len(adj_lines) == 2

    # Verify each adjustment has a corresponding line
    adjustment_ids_in_lines = {line.adjustment_id for line in adj_lines}
    adjustment_ids = {adj.id for adj in adjustments}

    assert adjustment_ids_in_lines == adjustment_ids

    # Verify line amounts match adjustment amounts (with sign for deductions)
    for line in adj_lines:
        adjustment = next(a for a in adjustments if a.id == line.adjustment_id)
        if adjustment.adjustment_type == AdjustmentType.DEDUCTION:
            expected_amount = -adjustment.amount
        else:
            expected_amount = adjustment.amount
        assert line.amount == expected_amount


def test_remittance_lines_integrity_amounts_sum_to_totals(
    clean_session: Session, test_user: User
):
    """
    Test that remittance line amounts sum to remittance totals.

    This verifies that gross_amount + adjustments_amount = sum of all line amounts
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-INTEGRITY-SUM",
    )
    clean_session.add(worklog)
    clean_session.flush()

    # Create segments
    segment1 = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    segment2 = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("60.00"),
        segment_date=date.today() + timedelta(days=1),
    )
    clean_session.add(segment1)
    clean_session.add(segment2)

    # Create adjustments
    bonus = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("100.00"),
        reason="Bonus",
    )
    penalty = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Penalty",
    )
    clean_session.add(bonus)
    clean_session.add(penalty)
    clean_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today() + timedelta(days=1)
    )

    remittance = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None

    # Get all remittance lines
    all_lines = clean_session.exec(
        select(RemittanceLine).where(RemittanceLine.remittance_id == remittance.id)
    ).all()

    # Sum all line amounts
    total_lines_amount = sum(line.amount for line in all_lines)

    # Expected: gross_amount + adjustments_amount = net_amount
    # Lines should sum to net_amount
    assert total_lines_amount == remittance.net_amount

    # Verify breakdown
    segment_lines = [line for line in all_lines if line.time_segment_id is not None]
    adjustment_lines = [line for line in all_lines if line.adjustment_id is not None]

    segment_total = sum(line.amount for line in segment_lines)
    adjustment_total = sum(line.amount for line in adjustment_lines)

    assert segment_total == remittance.gross_amount
    assert adjustment_total == remittance.adjustments_amount
    assert segment_total + adjustment_total == remittance.net_amount


def test_empty_adjustment_conditions_list(clean_session: Session, test_user: User):
    """
    Test edge case when adjustment_conditions list is empty.

    Scenario: All adjustments are already applied, so applied_adjustment_ids is empty
    but adjustment_conditions list might be empty, which could cause query issues.

    This tests the code path at line 128-131 in service.py
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-EMPTY-CONDITIONS",
    )
    clean_session.add(worklog)
    clean_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_session.add(segment)

    # Create adjustment
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Test",
    )
    clean_session.add(adjustment)
    clean_session.commit()

    # First settlement - applies adjustment
    settlement1 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    remittance1 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    # Mark as PAID (so adjustment is considered applied)
    remittance1.status = RemittanceStatus.PAID
    remittance1.paid_at = datetime.utcnow()
    clean_session.add(remittance1)
    clean_session.commit()

    # Second settlement - no new adjustments, adjustment_conditions might be empty
    # This should not cause errors
    settlement2 = SettlementService.generate_remittances_for_period(
        clean_session, date.today(), date.today()
    )

    # Should complete without errors
    assert settlement2 is not None

    # Should not create new remittance (work already paid, adjustment already applied)
    remittances2 = clean_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).all()

    # Should have 0 remittances or remittance with $0 net_amount
    total_new_payment = sum(r.net_amount for r in remittances2)
    assert total_new_payment == Decimal("0.00")


# Exception Handling


def test_generate_remittances_value_error(client: TestClient) -> None:
    """Test generate_remittances with ValueError."""
    with patch.object(
        SettlementService,
        "generate_remittances_for_period",
        side_effect=ValueError("Invalid date range"),
    ):
        response = client.post(
            "/api/v1/generate-remittances-for-all-users",
            params={
                "period_start": str(date.today()),
                "period_end": str(date.today()),
            },
        )
        assert response.status_code == 400
        assert "Invalid date range" in response.json()["detail"]


def test_generate_remittances_general_exception(client: TestClient) -> None:
    """Test generate_remittances with general exception."""
    with patch.object(
        SettlementService,
        "generate_remittances_for_period",
        side_effect=Exception("Unexpected error"),
    ):
        response = client.post(
            "/api/v1/generate-remittances-for-all-users",
            params={
                "period_start": str(date.today()),
                "period_end": str(date.today()),
            },
        )
        assert response.status_code == 500
        assert "Failed to generate remittances" in response.json()["detail"]


def test_list_worklogs_exception(client: TestClient) -> None:
    """Test list_worklogs with exception."""
    with patch.object(
        WorkLogService,
        "list_all_worklogs",
        side_effect=Exception("Database error"),
    ):
        response = client.get("/api/v1/list-all-worklogs")
        assert response.status_code == 500
        assert "Failed to list worklogs" in response.json()["detail"]
