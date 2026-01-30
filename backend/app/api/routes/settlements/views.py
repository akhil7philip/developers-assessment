"""
Settlement API routes.

Endpoints for generating remittances and listing worklogs.
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import SessionDep
from app.api.routes.settlements.service import SettlementService, WorkLogService
from app.models import (
    GenerateRemittancesResponse,
    SettlementPublic,
    WorkLogRemittanceFilter,
    WorkLogsPublic,
)

router = APIRouter(prefix="", tags=["settlements"])


@router.post(
    "/generate-remittances-for-all-users", response_model=GenerateRemittancesResponse
)
def generate_remittances_for_all_users(
    session: SessionDep,
    period_start: date = Query(..., description="Start date of settlement period"),
    period_end: date = Query(
        default=None, description="End date of settlement period (defaults to today)"
    ),
) -> Any:
    """
    Generate remittances for all users based on eligible work.

    This endpoint:
    - Finds all workers with unsettled time segments in the period
    - Calculates gross amounts from time segments
    - Applies retroactive adjustments from any period
    - Reconciles previously failed settlements
    - Creates a Settlement record with all Remittances

    The settlement run is idempotent - running it multiple times for the same
    period will not create duplicate remittances for already-paid work.

    **Query Parameters:**
    - `period_start`: Start date of the settlement period (required)
    - `period_end`: End date of the settlement period (optional, defaults to today)

    **Response:**
    Returns a settlement summary including:
    - Settlement record details
    - Number of remittances created
    - Total gross and net amounts
    - Confirmation message
    """
    # Default period_end to today if not provided
    if period_end is None:
        period_end = date.today()

    try:
        # Generate remittances
        settlement = SettlementService.generate_remittances_for_period(
            session, period_start, period_end
        )

        # Calculate totals from the settlement's remittances
        total_gross = sum(r.gross_amount for r in settlement.remittances)
        total_net = sum(r.net_amount for r in settlement.remittances)

        # Build response
        settlement_public = SettlementPublic(
            id=settlement.id,
            period_start=settlement.period_start,
            period_end=settlement.period_end,
            run_at=settlement.run_at,
            status=settlement.status.value,
            total_remittances_generated=settlement.total_remittances_generated,
        )

        return GenerateRemittancesResponse(
            settlement=settlement_public,
            remittances_created=settlement.total_remittances_generated,
            total_gross_amount=total_gross,
            total_net_amount=total_net,
            message=f"Successfully generated {settlement.total_remittances_generated} remittances for period {period_start} to {period_end}",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate remittances: {str(e)}"
        )


@router.get("/list-all-worklogs", response_model=WorkLogsPublic)
def list_all_worklogs(
    session: SessionDep,
    remittanceStatus: WorkLogRemittanceFilter | None = Query(
        default=None,
        description="Filter by remittance status: REMITTED or UNREMITTED",
    ),
    skip: int = Query(default=0, ge=0, description="Number of records to skip"),
    limit: int = Query(
        default=100, ge=1, le=1000, description="Maximum number of records to return"
    ),
) -> Any:
    """
    List all worklogs with filtering and amount information.

    This endpoint:
    - Lists all worklogs in the system
    - Optionally filters by remittance status
    - Calculates and includes the current amount for each worklog
    - Supports pagination

    **Query Parameters:**
    - `remittanceStatus`: Optional filter for remittance status
      - `REMITTED`: Only show worklogs where ALL time segments have been paid
      - `UNREMITTED`: Only show worklogs with at least one unpaid time segment
      - (omit): Show all worklogs
    - `skip`: Number of records to skip for pagination (default: 0)
    - `limit`: Maximum number of records to return (default: 100, max: 1000)

    **Response:**
    Returns a list of worklogs with:
    - All worklog details
    - `total_amount`: Calculated amount including all segments and adjustments
    - `is_remitted`: Whether the worklog is fully remitted
    - `count`: Total number of matching worklogs (for pagination)
    """
    try:
        return WorkLogService.list_all_worklogs(session, remittanceStatus, skip, limit)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list worklogs: {str(e)}"
        )
