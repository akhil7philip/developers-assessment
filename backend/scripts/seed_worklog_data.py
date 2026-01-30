"""
Seed data script for WorkLog Settlement System.

Creates realistic test scenarios demonstrating:
1. Simple happy path settlement
2. Retroactive adjustments
3. Failed settlement retry
4. Partial worklog settlement
5. Multi-month time segments
6. Time segment deletion

Run with: python -m scripts.seed_worklog_data
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from app.core.db import engine
from app.models import (
    Adjustment,
    AdjustmentType,
    Remittance,
    RemittanceLine,
    RemittanceStatus,
    Settlement,
    SettlementStatus,
    TimeSegment,
    User,
    WorkLog,
)


def create_test_users(session: Session) -> dict[str, uuid.UUID]:
    """Create test worker users."""
    users = {}

    # Find existing users or use first superuser
    existing_users = session.exec(select(User).limit(3)).all()

    if len(existing_users) >= 3:
        users["worker_a"] = existing_users[0].id
        users["worker_b"] = existing_users[1].id
        users["worker_c"] = existing_users[2].id
    else:
        # Use first available user repeatedly for testing
        if existing_users:
            users["worker_a"] = existing_users[0].id
            users["worker_b"] = existing_users[0].id
            users["worker_c"] = existing_users[0].id
        else:
            print("WARNING: No users found. Please create users first.")
            return {}

    return users


def seed_scenario_1_simple_happy_path(session: Session, worker_id: uuid.UUID) -> None:
    """
    Scenario 1: Simple Happy Path
    - Worker A logs 10 hours @ $50/hr = $500
    - Settlement run creates remittance with $500
    """
    print("Creating Scenario 1: Simple Happy Path...")

    # Create worklog
    worklog = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-001-SIMPLE",
    )
    session.add(worklog)
    session.flush()

    # Add time segments
    base_date = date(2026, 1, 15)
    for i in range(2):
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=base_date + timedelta(days=i),
            notes=f"Work on feature X - Day {i+1}",
        )
        session.add(segment)

    session.commit()
    print(f"✓ Created worklog {worklog.id} with 10 hours of work (2 segments)")


def seed_scenario_2_retroactive_adjustments(
    session: Session, worker_id: uuid.UUID
) -> None:
    """
    Scenario 2: Retroactive Adjustments
    - Month 1: Worker B logs 20 hours → Should be paid $1000
    - Month 2: Quality issue → $200 deduction applied
    - Month 2: Worker B logs 10 hours → Should be paid $500 - $200 = $300
    """
    print("Creating Scenario 2: Retroactive Adjustments...")

    # Month 1 worklog
    worklog_month1 = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-002-MONTH1",
    )
    session.add(worklog_month1)
    session.flush()

    # Month 1 time segments
    base_date_m1 = date(2026, 1, 5)
    for i in range(4):
        segment = TimeSegment(
            worklog_id=worklog_month1.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=base_date_m1 + timedelta(days=i * 2),
            notes=f"Month 1 work - Part {i+1}",
        )
        session.add(segment)

    # Simulate that Month 1 was settled and PAID
    settlement_m1 = Settlement(
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        run_at=datetime(2026, 2, 1, 0, 0, 0),
        status=SettlementStatus.COMPLETED,
        total_remittances_generated=1,
    )
    session.add(settlement_m1)
    session.flush()

    remittance_m1 = Remittance(
        settlement_id=settlement_m1.id,
        worker_user_id=worker_id,
        gross_amount=Decimal("1000.00"),
        adjustments_amount=Decimal("0.00"),
        net_amount=Decimal("1000.00"),
        status=RemittanceStatus.PAID,
        paid_at=datetime(2026, 2, 2, 0, 0, 0),
    )
    session.add(remittance_m1)
    session.flush()

    # Create remittance lines for paid segments
    segments_m1 = session.exec(
        select(TimeSegment).where(TimeSegment.worklog_id == worklog_month1.id)
    ).all()
    for segment in segments_m1:
        line = RemittanceLine(
            remittance_id=remittance_m1.id,
            time_segment_id=segment.id,
            amount=segment.hours_worked * segment.hourly_rate,
        )
        session.add(line)

    # Month 2: Add retroactive adjustment for Month 1 work
    adjustment = Adjustment(
        worklog_id=worklog_month1.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("200.00"),
        reason="Quality issue found in Month 1 deliverables",
    )
    session.add(adjustment)

    # Month 2: New worklog
    worklog_month2 = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-002-MONTH2",
    )
    session.add(worklog_month2)
    session.flush()

    # Month 2 time segments
    base_date_m2 = date(2026, 2, 5)
    for i in range(2):
        segment = TimeSegment(
            worklog_id=worklog_month2.id,
            hours_worked=Decimal("5.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=base_date_m2 + timedelta(days=i * 2),
            notes=f"Month 2 work - Part {i+1}",
        )
        session.add(segment)

    session.commit()
    print(f"✓ Created Month 1 worklog (PAID) with retroactive $200 deduction")
    print(f"✓ Created Month 2 worklog with 10 hours (should net $300 with adjustment)")


def seed_scenario_3_failed_settlement_retry(
    session: Session, worker_id: uuid.UUID
) -> None:
    """
    Scenario 3: Failed Settlement Retry
    - Month 1: Worker C settlement fails → Status FAILED
    - Month 2: New settlement includes Month 1 work + Month 2 work
    """
    print("Creating Scenario 3: Failed Settlement Retry...")

    # Month 1 worklog
    worklog_month1 = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-003-FAILED",
    )
    session.add(worklog_month1)
    session.flush()

    # Month 1 time segments
    base_date = date(2026, 1, 10)
    segments_m1 = []
    for i in range(3):
        segment = TimeSegment(
            worklog_id=worklog_month1.id,
            hours_worked=Decimal("8.00"),
            hourly_rate=Decimal("60.00"),
            segment_date=base_date + timedelta(days=i * 3),
            notes=f"Failed settlement work - Part {i+1}",
        )
        session.add(segment)
        session.flush()
        segments_m1.append(segment)

    # Create failed settlement
    settlement_failed = Settlement(
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        run_at=datetime(2026, 2, 1, 0, 0, 0),
        status=SettlementStatus.COMPLETED,
        total_remittances_generated=1,
    )
    session.add(settlement_failed)
    session.flush()

    remittance_failed = Remittance(
        settlement_id=settlement_failed.id,
        worker_user_id=worker_id,
        gross_amount=Decimal("1440.00"),  # 24 hours * $60
        adjustments_amount=Decimal("0.00"),
        net_amount=Decimal("1440.00"),
        status=RemittanceStatus.FAILED,  # Payment failed!
    )
    session.add(remittance_failed)
    session.flush()

    # Create remittance lines for failed remittance
    for segment in segments_m1:
        line = RemittanceLine(
            remittance_id=remittance_failed.id,
            time_segment_id=segment.id,
            amount=segment.hours_worked * segment.hourly_rate,
        )
        session.add(line)

    session.commit()
    print(f"✓ Created failed remittance for Worker C ($1440 unpaid)")
    print(f"  Next settlement should reconcile this failed payment")


def seed_scenario_4_partial_worklog_settlement(
    session: Session, worker_id: uuid.UUID
) -> None:
    """
    Scenario 4: Partial WorkLog Settlement
    - WorkLog has 3 time segments initially
    - First 2 segments get paid in Month 1
    - 3rd segment added later, should be paid in Month 2
    """
    print("Creating Scenario 4: Partial WorkLog Settlement...")

    # Create worklog
    worklog = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-004-PARTIAL",
    )
    session.add(worklog)
    session.flush()

    # Add first 2 time segments (will be paid)
    base_date = date(2026, 1, 8)
    segments_paid = []
    for i in range(2):
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("4.00"),
            hourly_rate=Decimal("45.00"),
            segment_date=base_date + timedelta(days=i),
            notes=f"Initial work - Day {i+1}",
        )
        session.add(segment)
        session.flush()
        segments_paid.append(segment)

    # Simulate paid settlement for first 2 segments
    settlement = Settlement(
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        run_at=datetime(2026, 2, 1, 0, 0, 0),
        status=SettlementStatus.COMPLETED,
        total_remittances_generated=1,
    )
    session.add(settlement)
    session.flush()

    remittance = Remittance(
        settlement_id=settlement.id,
        worker_user_id=worker_id,
        gross_amount=Decimal("360.00"),  # 8 hours * $45
        adjustments_amount=Decimal("0.00"),
        net_amount=Decimal("360.00"),
        status=RemittanceStatus.PAID,
        paid_at=datetime(2026, 2, 2, 0, 0, 0),
    )
    session.add(remittance)
    session.flush()

    for segment in segments_paid:
        line = RemittanceLine(
            remittance_id=remittance.id,
            time_segment_id=segment.id,
            amount=segment.hours_worked * segment.hourly_rate,
        )
        session.add(line)

    # Add 3rd segment later (unpaid)
    segment_unpaid = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("6.00"),
        hourly_rate=Decimal("45.00"),
        segment_date=date(2026, 2, 15),  # Added in Month 2
        notes="Additional work added later",
    )
    session.add(segment_unpaid)

    session.commit()
    print(f"✓ Created partially settled worklog")
    print(f"  - 2 segments PAID ($360)")
    print(f"  - 1 segment UNPAID ($270) - to be paid in next settlement")


def seed_scenario_5_multi_month_segments(
    session: Session, worker_id: uuid.UUID
) -> None:
    """
    Scenario 5: Multi-Month Time Segments
    - Time segments span across settlement period boundary
    - Proper date-based filtering
    """
    print("Creating Scenario 5: Multi-Month Time Segments...")

    # Create worklog
    worklog = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-005-MULTIMONTH",
    )
    session.add(worklog)
    session.flush()

    # Add segments spanning two months
    dates = [
        date(2026, 1, 28),  # End of Month 1
        date(2026, 1, 29),  # End of Month 1
        date(2026, 2, 1),  # Start of Month 2
        date(2026, 2, 2),  # Start of Month 2
    ]

    for i, segment_date in enumerate(dates):
        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("3.00"),
            hourly_rate=Decimal("55.00"),
            segment_date=segment_date,
            notes=f"Cross-month work - Day {segment_date}",
        )
        session.add(segment)

    session.commit()
    print(f"✓ Created worklog with segments spanning Jan-Feb boundary")


def seed_scenario_6_deleted_time_segment(
    session: Session, worker_id: uuid.UUID
) -> None:
    """
    Scenario 6: Time Segment Deletion
    - Time segment gets soft-deleted after initial logging
    - Should not appear in settlement calculations
    """
    print("Creating Scenario 6: Deleted Time Segment...")

    # Create worklog
    worklog = WorkLog(
        worker_user_id=worker_id,
        task_identifier="TASK-006-DELETED",
    )
    session.add(worklog)
    session.flush()

    # Add active segment
    segment_active = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2026, 1, 20),
        notes="Valid work",
    )
    session.add(segment_active)

    # Add soft-deleted segment (disputed/removed)
    segment_deleted = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date(2026, 1, 21),
        notes="Disputed work - removed",
        deleted_at=datetime(2026, 1, 25, 10, 0, 0),  # Soft deleted
    )
    session.add(segment_deleted)

    session.commit()
    print(f"✓ Created worklog with 1 active + 1 deleted segment")
    print(f"  Only $250 (5 hours) should be paid, not $400")


def main() -> None:
    """Run all seed scenarios."""
    print("\n" + "=" * 60)
    print("SEEDING WORKLOG SETTLEMENT SYSTEM TEST DATA")
    print("=" * 60 + "\n")

    with Session(engine) as session:
        # Get test users
        users = create_test_users(session)
        if not users:
            print("ERROR: Cannot seed data without users")
            return

        # Seed all scenarios
        seed_scenario_1_simple_happy_path(session, users["worker_a"])
        seed_scenario_2_retroactive_adjustments(session, users["worker_b"])
        seed_scenario_3_failed_settlement_retry(session, users["worker_c"])
        seed_scenario_4_partial_worklog_settlement(session, users["worker_a"])
        seed_scenario_5_multi_month_segments(session, users["worker_b"])
        seed_scenario_6_deleted_time_segment(session, users["worker_c"])

    print("\n" + "=" * 60)
    print("✓ SEED DATA CREATION COMPLETE")
    print("=" * 60 + "\n")
    print("Next steps:")
    print("1. Run: POST /api/v1/generate-remittances-for-all-users")
    print("   with period_start=2026-01-01&period_end=2026-01-31")
    print("2. Check: GET /api/v1/list-all-worklogs?remittanceStatus=UNREMITTED")
    print("3. Verify settlement calculations match expected values\n")


if __name__ == "__main__":
    main()
