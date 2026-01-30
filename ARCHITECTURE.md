# WorkLog Settlement System - Architecture Documentation

## Overview

This document explains the design decisions, architecture patterns, and implementation details of the WorkLog Settlement System - a backend feature for compensating independent workers based on time segments they've reported.

## Table of Contents

1. [Core Design Decisions](#core-design-decisions)
2. [Database Architecture](#database-architecture)
3. [Business Logic](#business-logic)
4. [API Design](#api-design)
5. [Financial Integrity](#financial-integrity)
6. [Performance Considerations](#performance-considerations)
7. [Testing Strategy](#testing-strategy)

---

## Core Design Decisions

### 1. Ledger-Based Approach

**Decision:** Use a `RemittanceLine` junction table to track exactly which work items were paid in which remittance.

**Rationale:**
- **Audit Trail**: Every payment can be traced back to specific time segments or adjustments
- **Double-Payment Prevention**: We can query if a time segment has already been paid
- **Financial Correctness**: Maintains integrity even as historical data changes
- **Reconciliation**: Failed settlements can be properly reconciled without losing track of what was attempted

**Implementation:**
```python
class RemittanceLine:
    remittance_id: FK to Remittance
    time_segment_id: FK to TimeSegment (optional)
    adjustment_id: FK to Adjustment (optional)
    amount: Decimal  # Amount attributed to this line
```

### 2. Decimal Precision for Money

**Decision:** Use `DECIMAL(10,2)` for all monetary values, not `FLOAT`.

**Rationale:**
- **Precision**: Floating-point arithmetic can introduce rounding errors (e.g., 0.1 + 0.2 != 0.3)
- **Financial Compliance**: Financial systems require exact decimal precision
- **Predictability**: Decimal arithmetic is deterministic and consistent

**Example:**
```python
hours_worked = Decimal("7.33")
hourly_rate = Decimal("45.67")
amount = hours_worked * hourly_rate  # Exactly 334.7611
```

### 3. Soft Delete for Time Segments

**Decision:** Implement soft delete using `deleted_at` timestamp instead of hard deletion.

**Rationale:**
- **Audit Compliance**: Regulatory requirements often mandate keeping historical records
- **Reversibility**: Mistakes can be corrected without data loss
- **Forensics**: Can investigate disputes by seeing full history
- **Settlement Integrity**: Segments deleted after being included in a settlement remain visible

**Implementation:**
```python
class TimeSegment:
    deleted_at: datetime | None = None  # NULL = active, timestamp = deleted
```

Queries always filter: `WHERE deleted_at IS NULL`

### 4. Separation of Settlement and Remittance

**Decision:** Create a `Settlement` entity separate from individual `Remittance` records.

**Rationale:**
- **Grouping**: All remittances from a single run are grouped together
- **Idempotency**: Can check if a settlement has already run for a period
- **Reporting**: Can generate settlement reports showing all payments in a run
- **Status Tracking**: Settlement-level status separate from individual payment status

---

## Database Architecture

### Entity Relationship Diagram

```
User (existing)
  └─> WorkLog (1:many)
        ├─> TimeSegment (1:many)
        └─> Adjustment (1:many)

Settlement
  └─> Remittance (1:many)
        ├─> User (many:1)
        └─> RemittanceLine (1:many)
              ├─> TimeSegment (many:1, optional)
              └─> Adjustment (many:1, optional)
```

### Key Relationships

1. **WorkLog → TimeSegment**: One-to-many with CASCADE delete
   - When a worklog is deleted, all its segments are deleted
   - Time segments cannot exist without a parent worklog

2. **Remittance → RemittanceLine**: One-to-many with CASCADE delete
   - Remittance lines are tightly coupled to their remittance
   - When a remittance is deleted, all lines go with it

3. **RemittanceLine → TimeSegment**: Many-to-one, nullable
   - A line item can reference a time segment OR an adjustment, not both
   - Allows independent tracking of work vs adjustments

### Indexes

Strategic indexes for query performance:

```sql
-- Most common query: find unsettled work for a worker
INDEX ON time_segment(worklog_id, segment_date, deleted_at)

-- Find remittances by status for reconciliation
INDEX ON remittance(worker_user_id, status)

-- Audit queries: find what was paid when
INDEX ON remittance_line(time_segment_id)
INDEX ON remittance_line(adjustment_id)
```

---

## Business Logic

### Settlement Calculation Flow

```
1. Input: period_start, period_end
2. Find all workers with eligible work
3. For each worker:
   a. Find unsettled time segments in period
   b. Find segments from failed settlements (any period)
   c. Find unapplied adjustments
   d. Calculate gross_amount (sum of segments)
   e. Calculate adjustments_amount (net of adjustments)
   f. net_amount = gross_amount + adjustments_amount
   g. Create Remittance with status PENDING
   h. Create RemittanceLine for each segment/adjustment
4. Return Settlement summary
```

### Retroactive Adjustment Handling

**Scenario:** Month 1 is settled and paid. In Month 2, a $200 deduction is added to Month 1 work.

**Solution:**
1. Adjustment is created with `worklog_id` pointing to Month 1 worklog
2. Settlement query finds "unapplied adjustments" - those not in a PAID remittance's lines
3. Month 2 settlement includes:
   - Month 2 new work: $500 gross
   - Month 1 retroactive adjustment: -$200
   - Net payment: $300

**Implementation:**
```python
def _find_applicable_adjustments(session, worker_id):
    # Find adjustments NOT in a PAID remittance
    paid_adjustment_ids = (
        SELECT adjustment_id FROM remittance_line
        JOIN remittance ON ...
        WHERE remittance.status = 'PAID'
    )
    
    return adjustments NOT IN paid_adjustment_ids
```

### Double-Payment Prevention

**Challenge:** How do we ensure a time segment is never paid twice?

**Solution:** Query segments that are NOT in a PAID remittance's lines:

```python
def _find_unsettled_time_segments(session, worker_id, period_start, period_end):
    # Subquery: IDs of segments already paid
    paid_segment_ids = (
        SELECT time_segment_id FROM remittance_line
        JOIN remittance ON ...
        WHERE remittance.status = 'PAID'
    )
    
    # Return segments NOT in paid list
    return time_segments WHERE id NOT IN paid_segment_ids
```

**Result:** Even if settlement is run multiple times, paid segments are excluded.

### Failed Settlement Reconciliation

**Scenario:** Payment processor fails, remittance marked as FAILED.

**Solution:**
1. Settlement query includes workers with FAILED remittances
2. Finds segments that were in failed remittance lines
3. Creates new remittance for same work in next settlement run
4. Previous failed remittance remains for audit, but work gets re-attempted

**Key:** The `status` field differentiates:
- **PENDING**: Created, not yet paid
- **PAID**: Successfully paid, segments now "consumed"
- **FAILED**: Payment failed, segments still need payment
- **CANCELLED**: Explicitly cancelled, segments back to unsettled

---

## API Design

### Endpoint 1: Generate Remittances

```
POST /api/v1/generate-remittances-for-all-users
  ?period_start=2026-01-01
  &period_end=2026-01-31  [optional, defaults to today]
```

**Design Decisions:**
1. **Query params vs body**: Simple dates fit better as query params
2. **Default period_end**: Convenience for "settle month to date"
3. **Idempotent**: Safe to retry; already-paid work excluded
4. **Atomic**: All remittances created in single transaction

**Response Includes:**
- Settlement record (with run metadata)
- Count of remittances created
- Total amounts (gross and net)
- Confirmation message

### Endpoint 2: List Worklogs

```
GET /api/v1/list-all-worklogs
  ?remittanceStatus=UNREMITTED  [optional: REMITTED | UNREMITTED]
  &skip=0
  &limit=100
```

**Design Decisions:**
1. **Filtering**: Optional filter allows "show me what needs payment"
2. **Amount calculation**: Included for every worklog (current state)
3. **Pagination**: Standard skip/limit pattern, max 1000 to prevent abuse
4. **Status definition**: 
   - REMITTED = ALL segments paid
   - UNREMITTED = AT LEAST ONE segment unpaid

---

## Financial Integrity

### Constraints

1. **No negative hours/rates**: CHECK constraints on time_segment
2. **Valid date ranges**: period_start <= period_end validation
3. **Foreign key integrity**: ON DELETE CASCADE where appropriate
4. **Enum values**: Database enums enforce valid statuses

### Audit Trail

Every financial transaction can be traced:

```sql
-- Who got paid what and when?
SELECT 
    u.email as worker,
    r.net_amount,
    r.paid_at,
    s.period_start,
    s.period_end
FROM remittance r
JOIN user u ON r.worker_user_id = u.id
JOIN settlement s ON r.settlement_id = s.id
WHERE r.status = 'PAID';

-- What specific work was in this payment?
SELECT
    ts.hours_worked,
    ts.hourly_rate,
    rl.amount,
    ts.segment_date
FROM remittance_line rl
JOIN time_segment ts ON rl.time_segment_id = ts.id
WHERE rl.remittance_id = '<remittance_id>';
```

### Reconciliation Checks

Daily/monthly jobs should verify:

```sql
-- No time segment paid twice
SELECT time_segment_id, COUNT(*) as payment_count
FROM remittance_line rl
JOIN remittance r ON rl.remittance_id = r.id
WHERE r.status = 'PAID' AND rl.time_segment_id IS NOT NULL
GROUP BY time_segment_id
HAVING COUNT(*) > 1;
-- Should return 0 rows

-- Remittance lines sum to remittance net_amount
SELECT r.id, r.net_amount, SUM(rl.amount) as lines_total
FROM remittance r
JOIN remittance_line rl ON rl.remittance_id = r.id
GROUP BY r.id, r.net_amount
HAVING r.net_amount != SUM(rl.amount);
-- Should return 0 rows
```

---

## Performance Considerations

### Query Optimization

1. **Indexes on foreign keys**: All FK columns indexed
2. **Composite indexes**: `(worklog_id, segment_date)` for date-range queries
3. **Status indexes**: Fast filtering by `remittance.status`

### Potential Bottlenecks

1. **Large worker count**: Settlement generation loops over all workers
   - **Mitigation**: Batch processing, async jobs for production
   
2. **Long time ranges**: Many segments to query
   - **Mitigation**: Limit period to 1 month at a time
   
3. **N+1 queries**: Fetching related data per worklog
   - **Mitigation**: Use SQLModel's `selectinload()` for eager loading

### Scaling Strategies

For production deployment with 10,000+ workers:

1. **Asynchronous settlement**: Background job queue (Celery, RQ)
2. **Batch processing**: Process workers in chunks of 100
3. **Read replicas**: Route list-worklogs queries to replica
4. **Caching**: Cache "is_remitted" status for recently queried worklogs
5. **Archival**: Move old settlements to archive tables after 2 years

---

## Testing Strategy

### Unit Tests (tests/services/)

Focus: Business logic in isolation

```python
test_calculate_gross_amount()  # Basic math
test_apply_retroactive_adjustments()  # Complex logic
test_prevent_double_payment()  # Critical integrity
test_decimal_precision()  # Financial correctness
test_soft_deleted_segments_excluded()  # Data integrity
```

### Integration Tests (tests/api_settlements/)

Focus: Full request/response cycle

```python
test_generate_remittances_success()  # Happy path
test_list_worklogs_filter_unremitted()  # Filtering
test_list_worklogs_amount_calculation()  # Computed fields
test_generate_remittances_invalid_dates()  # Validation
```

### Test Data Strategy

Seed data creates 6 realistic scenarios:
1. Simple happy path
2. Retroactive adjustments
3. Failed settlement retry
4. Partial worklog settlement
5. Multi-month segments
6. Soft-deleted segments

Each scenario tests a specific edge case.

---

## Future Enhancements

### Phase 2 Features

1. **Settlement preview**: Dry-run to show what would be paid
2. **Worker balance tracking**: Running total of owed vs paid
3. **Payment integration**: Actual payout via Stripe/PayPal
4. **Notification system**: Email workers when remittance is ready
5. **Dispute workflow**: Formal process for contesting segments/adjustments

### Performance Optimizations

1. **Materialized views**: Pre-compute worklog amounts
2. **Event sourcing**: Log all state changes for complete audit
3. **Partitioning**: Partition time_segment table by month
4. **Denormalization**: Cache computed totals on worklog table

---

## Conclusion

This architecture prioritizes:
- **Financial correctness** over development speed
- **Audit compliance** over storage optimization
- **Maintainability** over premature optimization
- **Idempotency** over performance

The design handles the core complexity: tracking financial state that changes retroactively while maintaining integrity and preventing common pitfalls like double-payment.

The ledger-based approach provides a solid foundation for future enhancements while ensuring the system can handle real-world edge cases from day one.
