"""
Settlement Service - Core business logic for generating worker remittances.

This service handles:
- Calculating worker remittances for a given period
- Finding unsettled time segments
- Applying retroactive adjustments
- Reconciling previously failed settlements
- Preventing double-payment
- Creating remittances with proper audit trails
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, and_, col, select

from app.models import (
    Adjustment,
    AdjustmentType,
    Remittance,
    RemittanceLine,
    RemittanceStatus,
    Settlement,
    SettlementStatus,
    TimeSegment,
    WorkLog,
    WorkLogPublic,
    WorkLogRemittanceFilter,
    WorkLogsPublic,
)


class SettlementService:
    """Service for generating and managing worker settlements."""

    @staticmethod
    def generate_remittances_for_period(
        session: Session, period_start: date, period_end: date
    ) -> Settlement:
        """
        Generate remittances for all workers with eligible work in the period.

        Args:
            session: Database session
            period_start: Start date of settlement period
            period_end: End date of settlement period

        Returns:
            Settlement record with summary information

        Raises:
            ValueError: If period_start > period_end
        """
        if period_start > period_end:
            raise ValueError("period_start must be <= period_end")

        # Create settlement record
        settlement = Settlement(
            period_start=period_start,
            period_end=period_end,
            run_at=datetime.utcnow(),
            status=SettlementStatus.COMPLETED,
            total_remittances_generated=0,
        )
        session.add(settlement)
        session.flush()  # Get settlement ID

        # Find all workers with unsettled work in this period
        workers_query = (
            select(TimeSegment.worklog_id)
            .join(WorkLog, col(TimeSegment.worklog_id) == col(WorkLog.id))
            .where(
                and_(
                    TimeSegment.segment_date >= period_start,
                    TimeSegment.segment_date <= period_end,
                    col(TimeSegment.deleted_at).is_(None),
                )
            )
            .distinct()
        )

        worklogs_with_work = session.exec(workers_query).all()

        # Get unique worker IDs
        worker_ids = set()
        for worklog_id in worklogs_with_work:
            worklog = session.get(WorkLog, worklog_id)
            if worklog:
                worker_ids.add(worklog.worker_user_id)

        # Also include workers with failed settlements from previous periods
        failed_remittances_query = (
            select(Remittance.worker_user_id)
            .where(Remittance.status == RemittanceStatus.FAILED)
            .distinct()
        )
        workers_with_failed = session.exec(failed_remittances_query).all()
        worker_ids.update(workers_with_failed)

        # Also include workers with unapplied adjustments (even if no time segments)
        # Subquery: adjustment IDs already applied in PAID remittances
        applied_adjustment_ids_query = (
            select(RemittanceLine.adjustment_id)
            .join(Remittance, col(RemittanceLine.remittance_id) == col(Remittance.id))
            .where(
                and_(
                    Remittance.status == RemittanceStatus.PAID,
                    col(RemittanceLine.adjustment_id).is_not(None),
                )
            )
        )
        applied_adjustment_ids = session.exec(applied_adjustment_ids_query).all()

        # Find workers with unapplied adjustments
        # Build conditions list
        adjustment_conditions = []
        if applied_adjustment_ids:
            adjustment_conditions.append(
                col(Adjustment.id).not_in(applied_adjustment_ids)
            )

        unapplied_adjustments_query = select(WorkLog.worker_user_id).join(
            Adjustment, col(Adjustment.worklog_id) == col(WorkLog.id)
        )
        if adjustment_conditions:
            unapplied_adjustments_query = unapplied_adjustments_query.where(
                and_(*adjustment_conditions)
            )
        unapplied_adjustments_query = unapplied_adjustments_query.distinct()

        workers_with_unapplied_adjustments = session.exec(
            unapplied_adjustments_query
        ).all()
        worker_ids.update(workers_with_unapplied_adjustments)

        remittances_created = 0

        # Generate remittance for each worker
        for worker_id in worker_ids:
            remittance = SettlementService._create_worker_remittance(
                session, worker_id, settlement.id, period_start, period_end
            )
            if remittance and remittance.net_amount != Decimal("0"):
                remittances_created += 1

        settlement.total_remittances_generated = remittances_created
        session.add(settlement)
        session.commit()
        session.refresh(settlement)

        return settlement

    @staticmethod
    def _create_worker_remittance(
        session: Session,
        worker_id: uuid.UUID,
        settlement_id: uuid.UUID,
        period_start: date,
        period_end: date,
    ) -> Remittance | None:
        """
        Create a remittance for a single worker.

        Handles:
        - Finding unsettled time segments
        - Finding applicable adjustments
        - Reconciling failed settlements
        - Creating remittance lines

        Args:
            session: Database session
            worker_id: Worker's user ID (UUID)
            settlement_id: ID of the settlement run (UUID)
            period_start: Start of settlement period
            period_end: End of settlement period

        Returns:
            Created remittance or None if no eligible work
        """
        # Find unsettled time segments for this worker in the period
        unsettled_segments = SettlementService._find_unsettled_time_segments(
            session, worker_id, period_start, period_end
        )

        # Find unsettled time segments from failed settlements (any period)
        failed_settlement_segments = (
            SettlementService._find_segments_from_failed_settlements(session, worker_id)
        )

        # Combine all segments and deduplicate by ID (can't use set() on SQLModel objects)
        segments_by_id = {}
        for segment in unsettled_segments + failed_settlement_segments:
            segments_by_id[segment.id] = segment
        all_segments = list(segments_by_id.values())

        # Find applicable adjustments (not yet applied)
        applicable_adjustments = SettlementService._find_applicable_adjustments(
            session, worker_id
        )

        # If no work and no adjustments, skip this worker
        if not all_segments and not applicable_adjustments:
            return None

        # Calculate amounts
        gross_amount = sum(
            (segment.hours_worked * segment.hourly_rate) for segment in all_segments
        ) or Decimal("0")

        adjustments_amount = Decimal("0")
        for adjustment in applicable_adjustments:
            if adjustment.adjustment_type == AdjustmentType.DEDUCTION:
                adjustments_amount -= adjustment.amount
            else:  # ADDITION
                adjustments_amount += adjustment.amount

        net_amount = gross_amount + adjustments_amount

        # Create remittance
        remittance = Remittance(
            settlement_id=settlement_id,
            worker_user_id=worker_id,
            gross_amount=gross_amount,
            adjustments_amount=adjustments_amount,
            net_amount=net_amount,
            status=RemittanceStatus.PENDING,
        )
        session.add(remittance)
        session.flush()  # Get remittance ID

        # Create remittance lines for time segments
        for segment in all_segments:
            line = RemittanceLine(
                remittance_id=remittance.id,
                time_segment_id=segment.id,
                adjustment_id=None,
                amount=segment.hours_worked * segment.hourly_rate,
            )
            session.add(line)

        # Create remittance lines for adjustments
        for adjustment in applicable_adjustments:
            amount = adjustment.amount
            if adjustment.adjustment_type == AdjustmentType.DEDUCTION:
                amount = -amount

            line = RemittanceLine(
                remittance_id=remittance.id,
                time_segment_id=None,
                adjustment_id=adjustment.id,
                amount=amount,
            )
            session.add(line)

        session.flush()
        return remittance

    @staticmethod
    def _find_unsettled_time_segments(
        session: Session, worker_id: uuid.UUID, period_start: date, period_end: date
    ) -> list[TimeSegment]:
        """
        Find time segments that haven't been paid yet.

        A segment is unsettled if:
        - It's not soft-deleted
        - It's in the settlement period
        - It hasn't been included in a PAID remittance

        Args:
            session: Database session
            worker_id: Worker's user ID (UUID)
            period_start: Start of period
            period_end: End of period

        Returns:
            List of unsettled time segments
        """
        # Subquery: segment IDs that are already paid
        paid_segment_ids_query = (
            select(RemittanceLine.time_segment_id)
            .join(Remittance, col(RemittanceLine.remittance_id) == col(Remittance.id))
            .where(
                and_(
                    Remittance.status == RemittanceStatus.PAID,
                    col(RemittanceLine.time_segment_id).is_not(None),
                )
            )
        )
        paid_segment_ids = session.exec(paid_segment_ids_query).all()

        # Build query conditions
        conditions: list[ColumnElement[bool] | bool] = [
            WorkLog.worker_user_id == worker_id,
            TimeSegment.segment_date >= period_start,
            TimeSegment.segment_date <= period_end,
            col(TimeSegment.deleted_at).is_(None),
        ]

        # Only add the paid segment filter if there are paid segments
        if paid_segment_ids:
            conditions.append(col(TimeSegment.id).not_in(paid_segment_ids))

        # Query unsettled segments
        query = (
            select(TimeSegment)
            .join(WorkLog, col(TimeSegment.worklog_id) == col(WorkLog.id))
            .where(and_(*conditions))
        )

        return list(session.exec(query).all())

    @staticmethod
    def _find_segments_from_failed_settlements(
        session: Session, worker_id: uuid.UUID
    ) -> list[TimeSegment]:
        """
        Find time segments that were in failed settlements.

        These segments need to be reconciled in the next settlement.

        Args:
            session: Database session
            worker_id: Worker's user ID (UUID)

        Returns:
            List of time segments from failed settlements
        """
        # Find failed remittances for this worker
        failed_remittances_query = select(Remittance.id).where(
            and_(
                Remittance.worker_user_id == worker_id,
                Remittance.status == RemittanceStatus.FAILED,
            )
        )
        failed_remittance_ids = session.exec(failed_remittances_query).all()

        if not failed_remittance_ids:
            return []

        # Find time segments from those remittances
        segment_ids_query = select(RemittanceLine.time_segment_id).where(
            and_(
                col(RemittanceLine.remittance_id).in_(failed_remittance_ids),
                col(RemittanceLine.time_segment_id).is_not(None),
            )
        )
        segment_ids = session.exec(segment_ids_query).all()

        if not segment_ids:
            return []

        # Get actual time segment objects
        segments_query = select(TimeSegment).where(
            and_(
                col(TimeSegment.id).in_(segment_ids),
                col(TimeSegment.deleted_at).is_(None),
            )
        )
        return list(session.exec(segments_query).all())

    @staticmethod
    def _find_applicable_adjustments(
        session: Session, worker_id: uuid.UUID
    ) -> list[Adjustment]:
        """
        Find adjustments that haven't been applied in a paid remittance.

        This includes retroactive adjustments from any period.

        Args:
            session: Database session
            worker_id: Worker's user ID (UUID)

        Returns:
            List of applicable adjustments
        """
        # Subquery: adjustment IDs already applied in PAID remittances
        applied_adjustment_ids_query = (
            select(RemittanceLine.adjustment_id)
            .join(Remittance, col(RemittanceLine.remittance_id) == col(Remittance.id))
            .where(
                and_(
                    Remittance.status == RemittanceStatus.PAID,
                    col(RemittanceLine.adjustment_id).is_not(None),
                )
            )
        )
        applied_adjustment_ids = session.exec(applied_adjustment_ids_query).all()

        # Query unapplied adjustments for this worker's worklogs
        query = (
            select(Adjustment)
            .join(WorkLog, col(Adjustment.worklog_id) == col(WorkLog.id))
            .where(
                and_(
                    WorkLog.worker_user_id == worker_id,
                    col(Adjustment.id).not_in(applied_adjustment_ids)
                    if applied_adjustment_ids
                    else True,
                )
            )
        )

        return list(session.exec(query).all())


class WorkLogService:
    """Service for managing worklogs and querying their status."""

    @staticmethod
    def list_all_worklogs(
        session: Session,
        remittance_filter: WorkLogRemittanceFilter | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> WorkLogsPublic:
        """
        List all worklogs with optional filtering by remittance status.

        Performance notes:
        - Caches remittance status to avoid duplicate calculations
        - For very large datasets (10k+ worklogs), consider pushing the
          remittance filter into SQL using a subquery/join to avoid
          loading all worklogs into memory

        Args:
            session: Database session
            remittance_filter: Optional filter (REMITTED or UNREMITTED)
            skip: Number of records to skip (pagination)
            limit: Maximum number of records to return

        Returns:
            WorkLogsPublic with data and count
        """
        # Base query
        query = select(WorkLog)

        # Get all worklogs first
        all_worklogs = session.exec(query).all()

        # Cache computed values to avoid duplicate calculations
        # Maps worklog_id -> (is_remitted: bool, amount: Decimal)
        worklog_cache: dict[uuid.UUID, tuple[bool, Decimal]] = {}

        # Compute and cache values once per worklog
        for worklog in all_worklogs:
            is_remitted = WorkLogService._is_worklog_remitted(session, worklog.id)
            amount = WorkLogService._calculate_worklog_amount(session, worklog.id)
            worklog_cache[worklog.id] = (is_remitted, amount)

        # Filter by remittance status if requested
        filtered_worklogs: list[WorkLog] = []
        for worklog in all_worklogs:
            is_remitted, _ = worklog_cache[worklog.id]

            if remittance_filter == WorkLogRemittanceFilter.REMITTED and is_remitted:
                filtered_worklogs.append(worklog)
            elif (
                remittance_filter == WorkLogRemittanceFilter.UNREMITTED
                and not is_remitted
            ):
                filtered_worklogs.append(worklog)
            elif remittance_filter is None:
                filtered_worklogs.append(worklog)

        # Apply pagination
        paginated_worklogs = filtered_worklogs[skip : skip + limit]

        # Convert to public models using cached values
        public_worklogs: list[WorkLogPublic] = []
        for worklog in paginated_worklogs:
            is_remitted, amount = worklog_cache[worklog.id]

            public_worklog = WorkLogPublic(
                id=worklog.id,
                worker_user_id=worklog.worker_user_id,
                task_identifier=worklog.task_identifier,
                created_at=worklog.created_at,
                updated_at=worklog.updated_at,
                total_amount=amount,
                is_remitted=is_remitted,
            )
            public_worklogs.append(public_worklog)

        return WorkLogsPublic(data=public_worklogs, count=len(filtered_worklogs))

    @staticmethod
    def _calculate_worklog_amount(session: Session, worklog_id: uuid.UUID) -> Decimal:
        """
        Calculate the total amount for a worklog.

        This includes:
        - All non-deleted time segments
        - All related adjustments

        Args:
            session: Database session
            worklog_id: ID of the worklog (UUID)

        Returns:
            Total amount (gross + adjustments)
        """
        # Calculate gross from time segments
        segments_query = select(TimeSegment).where(
            and_(
                TimeSegment.worklog_id == worklog_id,
                col(TimeSegment.deleted_at).is_(None),
            )
        )
        segments = session.exec(segments_query).all()

        gross_amount = sum(
            (segment.hours_worked * segment.hourly_rate) for segment in segments
        ) or Decimal("0")

        # Calculate adjustments
        adjustments_query = select(Adjustment).where(
            Adjustment.worklog_id == worklog_id
        )
        adjustments = session.exec(adjustments_query).all()

        adjustments_amount = Decimal("0")
        for adjustment in adjustments:
            if adjustment.adjustment_type == AdjustmentType.DEDUCTION:
                adjustments_amount -= adjustment.amount
            else:  # ADDITION
                adjustments_amount += adjustment.amount

        return gross_amount + adjustments_amount

    @staticmethod
    def _is_worklog_remitted(session: Session, worklog_id: uuid.UUID) -> bool:
        """
        Check if a worklog is fully remitted.

        A worklog is considered remitted if ALL of its non-deleted time segments
        have been paid (included in a PAID remittance).

        Args:
            session: Database session
            worklog_id: ID of the worklog (UUID)

        Returns:
            True if fully remitted, False otherwise
        """
        # Get all non-deleted time segments for this worklog
        segments_query = select(TimeSegment).where(
            and_(
                TimeSegment.worklog_id == worklog_id,
                col(TimeSegment.deleted_at).is_(None),
            )
        )
        segments = session.exec(segments_query).all()

        # If no segments, consider it unremitted
        if not segments:
            return False

        # Get all segment IDs for this worklog
        segment_ids = {segment.id for segment in segments}

        # Get all segment IDs that have been paid in a single query
        # This avoids N+1 queries by fetching all paid segments at once
        paid_segment_ids_query = (
            select(RemittanceLine.time_segment_id)
            .join(Remittance, col(RemittanceLine.remittance_id) == col(Remittance.id))
            .where(
                and_(
                    col(RemittanceLine.time_segment_id).in_(segment_ids),
                    Remittance.status == RemittanceStatus.PAID,
                )
            )
        )
        paid_segment_ids = set(session.exec(paid_segment_ids_query).all())

        # Check if all segments have been paid
        # A worklog is fully remitted only if ALL its segments are in paid_segment_ids
        return segment_ids.issubset(paid_segment_ids)
