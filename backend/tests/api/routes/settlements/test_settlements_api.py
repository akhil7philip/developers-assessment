"""
API Endpoint Integration Tests for WorkLog Settlement System.

Tests the actual HTTP endpoints, not just the service layer.

Endpoints tested:
1. POST /generate-remittances-for-all-users
2. GET /list-all-worklogs
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.routes.settlements.service import SettlementService
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
def clean_db():
    """Clean database before and after tests."""
    with Session(engine) as session:
        _cleanup_all_data(session)
        yield session
        _cleanup_all_data(session)


@pytest.fixture
def test_user(clean_db: Session) -> User:
    """Create a test user."""
    import uuid as uuid_lib

    user = User(
        email=f"api_test_{uuid_lib.uuid4()}@example.com",
        hashed_password="dummy",
        is_active=True,
        is_superuser=False,
    )
    clean_db.add(user)
    clean_db.commit()
    clean_db.refresh(user)
    return user


def test_generate_remittances_endpoint_success(clean_db: Session, test_user: User):
    """
    Test POST /generate-remittances-for-all-users returns correct response.
    """
    # Create worklog with time segment
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="API-TEST-1",
    )
    clean_db.add(worklog)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("8.00"),
        hourly_rate=Decimal("75.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)
    clean_db.commit()

    # Call API endpoint
    response = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={
            "period_start": str(date.today()),
            "period_end": str(date.today()),
        },
    )

    # Verify response
    assert response.status_code == 200
    
    data = response.json()
    
    # Check structure
    assert "settlement" in data
    assert "remittances_created" in data
    assert "total_gross_amount" in data
    assert "total_net_amount" in data
    assert "message" in data
    
    # Check values
    assert data["remittances_created"] == 1
    assert Decimal(data["total_gross_amount"]) == Decimal("600.00")
    assert Decimal(data["total_net_amount"]) == Decimal("600.00")
    
    # Check settlement details
    settlement = data["settlement"]
    assert "id" in settlement
    assert settlement["period_start"] == str(date.today())
    assert settlement["period_end"] == str(date.today())
    assert settlement["status"] == "COMPLETED"
    assert settlement["total_remittances_generated"] == 1


def test_generate_remittances_endpoint_default_period_end(clean_db: Session, test_user: User):
    """
    Test that period_end defaults to today if not provided.
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="API-TEST-DEFAULT",
    )
    clean_db.add(worklog)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)
    clean_db.commit()

    # Call without period_end
    response = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={
            "period_start": str(date.today()),
            # period_end omitted
        },
    )

    assert response.status_code == 200
    data = response.json()
    
    # Should use today as period_end
    assert data["settlement"]["period_end"] == str(date.today())
    assert data["remittances_created"] == 1


def test_generate_remittances_endpoint_invalid_period(clean_db: Session):
    """
    Test that invalid date range returns 400 error.
    """
    response = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={
            "period_start": str(date.today()),
            "period_end": str(date.today() - timedelta(days=1)),  # Before start!
        },
    )

    assert response.status_code == 400
    assert "period_start must be <= period_end" in response.json()["detail"]


def test_generate_remittances_endpoint_empty_period(clean_db: Session):
    """
    Test endpoint with no work in period.
    """
    future_date = date.today() + timedelta(days=365)
    
    response = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={
            "period_start": str(future_date),
            "period_end": str(future_date),
        },
    )

    assert response.status_code == 200
    data = response.json()
    
    assert data["remittances_created"] == 0
    assert Decimal(data["total_gross_amount"]) == Decimal("0.00")
    assert Decimal(data["total_net_amount"]) == Decimal("0.00")


def test_generate_remittances_endpoint_with_adjustments(clean_db: Session, test_user: User):
    """
    Test endpoint calculates totals correctly with adjustments.
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="API-TEST-ADJ",
    )
    clean_db.add(worklog)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)

    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Penalty",
    )
    clean_db.add(adjustment)
    clean_db.commit()

    response = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={
            "period_start": str(date.today()),
            "period_end": str(date.today()),
        },
    )

    assert response.status_code == 200
    data = response.json()
    
    # Gross should be 500, net should be 450 (after deduction)
    assert Decimal(data["total_gross_amount"]) == Decimal("500.00")
    assert Decimal(data["total_net_amount"]) == Decimal("450.00")


def test_list_worklogs_endpoint_basic(clean_db: Session, test_user: User):
    """
    Test GET /list-all-worklogs returns correct structure.
    """
    # Create worklog
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="LIST-TEST-1",
    )
    clean_db.add(worklog)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("6.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)
    clean_db.commit()

    # Call API
    response = client.get("/api/v1/list-all-worklogs")

    assert response.status_code == 200
    
    data = response.json()
    
    # Check structure
    assert "data" in data
    assert "count" in data
    
    # Check worklog exists
    assert data["count"] >= 1
    
    # Find our worklog
    our_worklog = next(
        (wl for wl in data["data"] if wl["task_identifier"] == "LIST-TEST-1"),
        None
    )
    
    assert our_worklog is not None
    assert "id" in our_worklog
    assert "worker_user_id" in our_worklog
    assert "task_identifier" in our_worklog
    assert "total_amount" in our_worklog
    assert "is_remitted" in our_worklog
    assert "created_at" in our_worklog
    assert "updated_at" in our_worklog
    
    # Check calculated values
    assert Decimal(our_worklog["total_amount"]) == Decimal("300.00")
    assert our_worklog["is_remitted"] == False  # Not paid yet


def test_list_worklogs_endpoint_filter_remitted(clean_db: Session, test_user: User):
    """
    Test filtering by REMITTED status.
    """
    # Create and pay a worklog
    worklog_paid = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="PAID-WORKLOG",
    )
    clean_db.add(worklog_paid)
    clean_db.flush()

    segment_paid = TimeSegment(
        worklog_id=worklog_paid.id,
        hours_worked=Decimal("5.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment_paid)
    clean_db.commit()

    # Generate and pay remittance
    settlement = SettlementService.generate_remittances_for_period(
        clean_db, date.today(), date.today()
    )
    
    remittance = clean_db.exec(
        select(Remittance).where(Remittance.settlement_id == settlement.id)
    ).first()
    
    remittance.status = RemittanceStatus.PAID
    remittance.paid_at = datetime.utcnow()
    clean_db.add(remittance)
    clean_db.commit()

    # Filter by REMITTED
    response = client.get(
        "/api/v1/list-all-worklogs",
        params={"remittanceStatus": "REMITTED"},
    )

    assert response.status_code == 200
    data = response.json()
    
    # Should include paid worklog
    identifiers = [wl["task_identifier"] for wl in data["data"]]
    assert "PAID-WORKLOG" in identifiers
    
    # All worklogs should be remitted
    for wl in data["data"]:
        assert wl["is_remitted"] == True


def test_list_worklogs_endpoint_filter_unremitted(clean_db: Session, test_user: User):
    """
    Test filtering by UNREMITTED status.
    """
    # Create unpaid worklog
    worklog_unpaid = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="UNPAID-WORKLOG",
    )
    clean_db.add(worklog_unpaid)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog_unpaid.id,
        hours_worked=Decimal("3.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)
    clean_db.commit()

    # Filter by UNREMITTED
    response = client.get(
        "/api/v1/list-all-worklogs",
        params={"remittanceStatus": "UNREMITTED"},
    )

    assert response.status_code == 200
    data = response.json()
    
    # Should include unpaid worklog
    identifiers = [wl["task_identifier"] for wl in data["data"]]
    assert "UNPAID-WORKLOG" in identifiers
    
    # All worklogs should be unremitted
    for wl in data["data"]:
        assert wl["is_remitted"] == False


def test_list_worklogs_endpoint_pagination(clean_db: Session, test_user: User):
    """
    Test pagination parameters work correctly.
    """
    # Create 5 worklogs
    for i in range(5):
        worklog = WorkLog(
            worker_user_id=test_user.id,
            task_identifier=f"PAGINATE-{i}",
        )
        clean_db.add(worklog)
        clean_db.flush()

        segment = TimeSegment(
            worklog_id=worklog.id,
            hours_worked=Decimal("1.00"),
            hourly_rate=Decimal("50.00"),
            segment_date=date.today(),
        )
        clean_db.add(segment)
    clean_db.commit()

    # Test limit
    response = client.get(
        "/api/v1/list-all-worklogs",
        params={"skip": 0, "limit": 3},
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return at most 3 items
    assert len(data["data"]) <= 3
    # But count should be total
    assert data["count"] >= 5

    # Test skip
    response2 = client.get(
        "/api/v1/list-all-worklogs",
        params={"skip": 2, "limit": 2},
    )
    
    assert response2.status_code == 200
    data2 = response2.json()
    
    # Should return different items
    assert len(data2["data"]) <= 2


def test_list_worklogs_endpoint_with_adjustments(clean_db: Session, test_user: User):
    """
    Test that total_amount includes adjustments.
    """
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="AMOUNT-TEST",
    )
    clean_db.add(worklog)
    clean_db.flush()

    segment = TimeSegment(
        worklog_id=worklog.id,
        hours_worked=Decimal("10.00"),
        hourly_rate=Decimal("50.00"),
        segment_date=date.today(),
    )
    clean_db.add(segment)

    bonus = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.ADDITION,
        amount=Decimal("100.00"),
        reason="Bonus",
    )
    clean_db.add(bonus)
    clean_db.commit()

    response = client.get("/api/v1/list-all-worklogs")

    assert response.status_code == 200
    data = response.json()
    
    our_worklog = next(
        (wl for wl in data["data"] if wl["task_identifier"] == "AMOUNT-TEST"),
        None
    )
    
    assert our_worklog is not None
    # Should be 500 + 100 = 600
    assert Decimal(our_worklog["total_amount"]) == Decimal("600.00")


def test_list_worklogs_endpoint_empty(clean_db: Session):
    """
    Test endpoint returns empty list when no worklogs exist.
    """
    response = client.get("/api/v1/list-all-worklogs")

    assert response.status_code == 200
    data = response.json()
    
    assert "data" in data
    assert "count" in data
    assert data["count"] == 0
    assert len(data["data"]) == 0


def test_list_worklogs_endpoint_invalid_limit(clean_db: Session):
    """
    Test that invalid limit returns 422 validation error.
    """
    # Limit too high (max is 1000)
    response = client.get(
        "/api/v1/list-all-worklogs",
        params={"limit": 2000},
    )

    assert response.status_code == 422  # Validation error


def test_endpoints_are_mounted_correctly(clean_db: Session):
    """
    Test that both endpoints are accessible at correct paths.
    """
    # Test generate-remittances exists
    response1 = client.post(
        "/api/v1/generate-remittances-for-all-users",
        params={"period_start": str(date.today())},
    )
    # Should not be 404
    assert response1.status_code != 404

    # Test list-all-worklogs exists
    response2 = client.get("/api/v1/list-all-worklogs")
    # Should not be 404
    assert response2.status_code != 404


def test_list_worklogs_amount_calculation(clean_db: Session, test_user: User):
    """Test that amount is calculated correctly for each worklog."""
    # Create worklog with multiple segments
    worklog = WorkLog(
        worker_user_id=test_user.id,
        task_identifier="API-TEST-AMOUNT",
    )
    clean_db.add(worklog)
    clean_db.flush()

    # Add 2 segments
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
        clean_db.add(segment)

    # Add adjustment
    adjustment = Adjustment(
        worklog_id=worklog.id,
        adjustment_type=AdjustmentType.DEDUCTION,
        amount=Decimal("50.00"),
        reason="Test deduction",
    )
    clean_db.add(adjustment)
    clean_db.commit()

    # Call API
    response = client.get("/api/v1/list-all-worklogs")

    assert response.status_code == 200
    data = response.json()

    # Find our worklog
    our_worklog = next(
        (wl for wl in data["data"] if wl["task_identifier"] == "API-TEST-AMOUNT"),
        None,
    )

    assert our_worklog is not None
    # Expected: (5*50) + (3*60) - 50 = 250 + 180 - 50 = 380
    assert Decimal(our_worklog["total_amount"]) == Decimal("380.00")
