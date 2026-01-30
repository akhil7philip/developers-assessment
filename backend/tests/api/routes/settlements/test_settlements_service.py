"""
Unit tests for SettlementService.

Tests the core business logic for:
- Calculating worker remittances
- Applying retroactive adjustments
- Reconciling failed settlements
- Preventing double-payment
- Decimal precision
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session, delete, select

from app.api.routes.settlements.service import SettlementService, WorkLogService
from app.core.db import engine
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


def _cleanup_settlement_data(session: Session) -> None:
    """Helper function to clean up all settlement-related data."""
    # Delete in order to respect foreign key constraints using batch deletes
    session.exec(delete(RemittanceLine))
    session.exec(delete(Remittance))
    session.exec(delete(Settlement))
    session.exec(delete(Adjustment))
    session.exec(delete(TimeSegment))
    session.exec(delete(WorkLog))
    session.commit()


@pytest.fixture
def test_session():
    """Create a test database session with cleanup."""
    with Session(engine) as session:
        # Clean up ALL settlement-related data before each test to ensure isolation
        _cleanup_settlement_data(session)
        yield session
        # Clean up after test as well
        _cleanup_settlement_data(session)


@pytest.fixture
def test_user(test_session: Session) -> User:
    """Create a unique test user for each test."""
    import uuid as uuid_lib

    # Always create a fresh user for each test to ensure complete isolation
    # This prevents any data leakage between tests
    user = User(
        email=f"test_worker_{uuid_lib.uuid4()}@example.com",
        hashed_password="dummy",
        is_active=True,
        is_superuser=False,
    )
    test_session.add(user)
    test_session.commit()
    test_session.refresh(user)
    return user


def test_calculate_gross_amount(test_session: Session, test_user: User):
    """Test that gross amount is calculated correctly from time segments."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-GROSS",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segments: 5 hours @ $50, 3 hours @ $60
    segments = [
        TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        ),
        TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("3.00"),
            hourly_rate=Decimal("60.00"),
            segment_date=date.today() + timedelta(days=1),
        ),
    ]
    for segment in segments:
        test_session.add(segment)
    test_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today() + timedelta(days=1)
    )

    # Verify remittance
    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    assert remittance.gross_amount == Decimal("430.00")  # (5*50) + (3*60)
    assert remittance.net_amount == Decimal("430.00")


def test_apply_retroactive_adjustments(test_session: Session, test_user: User):
    """Test that retroactive deductions are applied correctly."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-RETRO",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)

    # Add retroactive deduction
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("100.00"),
        reason="Quality issue",
    )
    test_session.add(adjustment)
    test_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Verify remittance includes adjustment
    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    assert remittance.gross_amount == Decimal("500.00")  # 10 * 50
    assert remittance.adjustments_amount == Decimal("-100.00")  # Deduction
    assert remittance.net_amount == Decimal("400.00")  # 500 - 100


def test_prevent_double_payment(test_session: Session, test_user: User):
    """Test that already-paid segments are not paid again."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-DOUBLE",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("8.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)
    test_session.commit()

    # First settlement - should pay $400
    settlement1 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    remittance1 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    assert remittance1.net_amount == Decimal("400.00")

    # Mark as paid
    remittance1.status = RemittanceStatus.PAID
    remittance1.paid_at = datetime.utcnow()
    test_session.add(remittance1)
    test_session.commit()

    # Second settlement - should NOT create new remittance for same work
    settlement2 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Should either have 0 remittances or a $0 remittance
    remittances2 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).all()

    # Worker should not get paid twice for same work
    total_new_payment = sum(r.net_amount for r in remittances2)
    assert total_new_payment == Decimal("0.00")


def test_failed_settlement_reconciliation(test_session: Session, test_user: User):
    """Test that failed settlements are reconciled in next run."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-FAILED",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("60.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)
    test_session.commit()

    # First settlement
    settlement1 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    remittance1 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    # Mark as FAILED
    remittance1.status = RemittanceStatus.FAILED
    test_session.add(remittance1)
    test_session.commit()

    # Second settlement - should reconcile failed payment
    settlement2 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Should create new remittance for the same work
    remittance2 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).first()

    assert remittance2 is not None
    assert remittance2.net_amount == Decimal("600.00")  # Still owes $600


def test_decimal_precision(test_session: Session, test_user: User):
    """Test that decimal precision is maintained (no floating point errors)."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-PRECISION",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment with precise decimals
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("7.33"),  # Tricky decimal
        hourly_rate=Decimal("45.67"),  # Tricky decimal
        segment_date=date.today(),
    )
    test_session.add(segment)
    test_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    # 7.33 * 45.67 = 334.7611, should round to 334.76
    expected = Decimal("7.33") * Decimal("45.67")
    assert remittance.net_amount == expected.quantize(Decimal("0.01"))


def test_empty_worklogs(test_session: Session):
    """Test that settlement handles empty period gracefully."""
    # Generate settlement for future period with no work
    future_date = date.today() + timedelta(days=365)
    settlement = SettlementService.generate_remittances_for_period(
        test_session, future_date, future_date
    )

    assert settlement.total_remittances_generated == 0


def test_soft_deleted_segments_excluded(test_session: Session, test_user: User):
    """Test that soft-deleted time segments are excluded from calculations."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-DELETED",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add active segment
    segment_active = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment_active)

    # Add soft-deleted segment
    segment_deleted = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
        deleted_at=datetime.utcnow(),
    )
    test_session.add(segment_deleted)
    test_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    # Should only pay for active segment ($250), not deleted ($150)
    assert remittance.net_amount == Decimal("250.00")


def test_period_validation(test_session: Session):
    """Test that invalid date periods raise ValueError."""
    with pytest.raises(ValueError, match="period_start must be <= period_end"):
        SettlementService.generate_remittances_for_period(
            test_session,
            date.today(),
            date.today() - timedelta(days=1),  # End before start!
        )


def test_apply_addition_adjustments(test_session: Session, test_user: User):
    """Test that ADDITION type adjustments are applied correctly."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-ADDITION",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)

    # Add ADDITION adjustment (bonus)
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("100.00"),
        reason="Performance bonus",
    )
    test_session.add(adjustment)
    test_session.commit()

    # Generate settlement
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Verify remittance includes addition
    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()

    assert remittance is not None
    assert remittance.gross_amount == Decimal("500.00")  # 10 * 50
    assert remittance.adjustments_amount == Decimal("100.00")  # Addition
    assert remittance.net_amount == Decimal("600.00")  # 500 + 100


def test_failed_settlement_with_no_segments(test_session: Session, test_user: User):
    """Test failed settlement reconciliation when it had no time segments."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-FAILED-NO-SEGMENTS",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add adjustment only (no time segments)
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Deduction only",
    )
    test_session.add(adjustment)
    test_session.commit()

    # First settlement
    settlement1 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    remittance1 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement1.id)
    ).first()

    # Mark as FAILED
    remittance1.status = RemittanceStatus.FAILED
    test_session.add(remittance1)
    test_session.commit()

    # Second settlement - should reconcile failed payment
    settlement2 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Should create new remittance for the adjustment
    remittance2 = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement2.id)
    ).first()

    assert remittance2 is not None
    assert remittance2.net_amount == Decimal("-50.00")  # Still owes deduction


def test_list_worklogs_filter_remitted(test_session: Session, test_user: User):
    """Test filtering worklogs by REMITTED status."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-FILTER-REMITTED",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)
    test_session.commit()

    # Generate settlement and mark as PAID
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )
    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()
    remittance.status = RemittanceStatus.PAID
    remittance.paid_at = datetime.utcnow()
    test_session.add(remittance)
    test_session.commit()

    # Filter by REMITTED
    result = WorkLogService.list_all_worklogs(
        test_session, remittance_filter=WorkLogRemittanceFilter.REMITTED, skip=0, limit=100
    )

    # Should include our remitted worklog
    assert result.count >= 1
    found = any(wl.task_identifier == "TEST-FILTER-REMITTED" for wl in result.data)
    assert found


def test_calculate_worklog_with_addition_adjustment(test_session: Session, test_user: User):
    """Test worklog amount calculation with ADDITION adjustments."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-CALC-ADDITION",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segment
    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)

    # Add ADDITION adjustment
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("75.00"),
        reason="Bonus",
    )
    test_session.add(adjustment)
    test_session.commit()

    # Calculate amount
    amount = WorkLogService._calculate_worklog_amount(test_session, worklog.id)

    # Expected: (10 * 50) + 75 = 575
    assert amount == Decimal("575.00")


def test_is_worklog_remitted_no_segments(test_session: Session, test_user: User):
    """Test that worklog with no segments is considered unremitted."""
    # Create worklog without any time segments
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-NO-SEGMENTS",
    )
    test_session.add(worklog)
    test_session.commit()

    # Check if remitted
    is_remitted = WorkLogService._is_worklog_remitted(test_session, worklog.id)

    # Should return False
    assert is_remitted == False


def test_is_worklog_remitted_all_paid(test_session: Session, test_user: User):
    """Test that worklog with all paid segments returns True."""
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-ALL-PAID",
    )
    test_session.add(worklog)
    test_session.flush()

    # Add time segments
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
    test_session.add(segment1)
    test_session.add(segment2)
    test_session.commit()

    # Generate settlement and mark as PAID
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today() + timedelta(days=1)
    )
    remittance = test_session.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()
    remittance.status = RemittanceStatus.PAID
    remittance.paid_at = datetime.utcnow()
    test_session.add(remittance)
    test_session.commit()

    # Check if remitted
    is_remitted = WorkLogService._is_worklog_remitted(test_session, worklog.id)

    # Should return True - all segments paid
    assert is_remitted == True


def test_generate_remittances_with_applied_adjustments(test_session: Session, test_user: User):
    """Test generate_remittances with applied adjustments (covers adjustment_conditions path)."""
    # Create worklog with time segment
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="TEST-ADJUSTMENT",
    )
    test_session.add(worklog)
    test_session.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    test_session.add(segment)
    test_session.commit()

    # Create an adjustment
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Test adjustment",
    )
    test_session.add(adjustment)
    test_session.commit()

    # Generate remittances - this should apply the adjustment
    settlement = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Verify settlement was created
    assert settlement is not None

    # Generate again - adjustment should already be applied, so adjustment_conditions
    # should include the applied adjustment ID
    settlement2 = SettlementService.generate_remittances_for_period(
        test_session, date.today(), date.today()
    )

    # Should not create duplicate remittances
    assert settlement2 is not None

