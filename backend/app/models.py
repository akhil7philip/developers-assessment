import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import EmailStr
from sqlalchemy import DECIMAL, Index
from sqlmodel import Column, Field, Relationship, SQLModel


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRegister(SQLModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on update, all are optional
class UserUpdate(UserBase):
    email: EmailStr | None = Field(default=None, max_length=255)  # type: ignore
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = Field(default=None, max_length=255)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    items: list["Item"] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(ItemBase):
    title: str | None = Field(default=None, min_length=1, max_length=255)  # type: ignore


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


# WorkLog Settlement System Models


class RemittanceStatus(str, Enum):
    """Status of a remittance payment."""

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AdjustmentType(str, Enum):
    """Type of adjustment applied to a worklog."""

    DEDUCTION = "DEDUCTION"
    ADDITION = "ADDITION"


class SettlementStatus(str, Enum):
    """Status of a settlement run."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WorkLogRemittanceFilter(str, Enum):
    """Filter options for worklog remittance status."""

    REMITTED = "REMITTED"
    UNREMITTED = "UNREMITTED"


# WorkLog Models


class WorkLogBase(SQLModel):
    task_identifier: str = Field(max_length=255)


class WorkLogCreate(WorkLogBase):
    worker_user_id: uuid.UUID


class WorkLogPublic(WorkLogBase):
    id: uuid.UUID
    worker_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # Computed fields
    total_amount: Decimal | None = None
    is_remitted: bool | None = None


class WorkLogsPublic(SQLModel):
    data: list[WorkLogPublic]
    count: int


# TimeSegment Models


class TimeSegmentBase(SQLModel):
    hours_worked: Decimal = Field(ge=0, le=9999.99, decimal_places=2)
    hourly_rate: Decimal = Field(ge=0, le=9999.99, decimal_places=2)
    segment_date: date
    notes: str | None = Field(default=None, max_length=1000)


class TimeSegmentCreate(TimeSegmentBase):
    worklog_id: uuid.UUID


class TimeSegmentPublic(TimeSegmentBase):
    id: uuid.UUID
    worklog_id: uuid.UUID
    gross_amount: Decimal
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TimeSegmentsPublic(SQLModel):
    data: list[TimeSegmentPublic]
    count: int


# Adjustment Models


class AdjustmentBase(SQLModel):
    adjustment_type: AdjustmentType
    amount: Decimal = Field(ge=0, le=9999.99, decimal_places=2)
    reason: str = Field(max_length=500)


class AdjustmentCreate(AdjustmentBase):
    worklog_id: uuid.UUID


class AdjustmentPublic(AdjustmentBase):
    id: uuid.UUID
    worklog_id: uuid.UUID
    created_at: datetime


class AdjustmentsPublic(SQLModel):
    data: list[AdjustmentPublic]
    count: int


# Settlement Models


class SettlementPublic(SQLModel):
    id: uuid.UUID
    period_start: date
    period_end: date
    run_at: datetime
    status: str  # SettlementStatus enum value as string
    total_remittances_generated: int


# Remittance Models


class RemittanceLinePublic(SQLModel):
    id: uuid.UUID
    time_segment_id: uuid.UUID | None
    adjustment_id: uuid.UUID | None
    amount: Decimal


class RemittancePublic(SQLModel):
    id: uuid.UUID
    settlement_id: uuid.UUID
    worker_user_id: uuid.UUID
    gross_amount: Decimal
    adjustments_amount: Decimal
    net_amount: Decimal
    status: str  # RemittanceStatus enum value as string
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    # Optional breakdown
    lines: list[RemittanceLinePublic] | None = None


class RemittancesPublic(SQLModel):
    data: list[RemittancePublic]
    count: int


# Generate Remittances Response


class GenerateRemittancesResponse(SQLModel):
    settlement: SettlementPublic
    remittances_created: int
    total_gross_amount: Decimal
    total_net_amount: Decimal
    message: str


# WorkLog Database Models


class WorkLog(SQLModel, table=True):
    """
    Container for all work done against a task by a worker.
    A worklog can contain multiple time segments recorded over time.
    """

    __tablename__ = "worklog"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    worker_user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, index=True)
    task_identifier: str = Field(max_length=255, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": lambda: datetime.utcnow()},
    )

    # Relationships
    time_segments: list["TimeSegment"] = Relationship(
        back_populates="worklog", cascade_delete=True
    )
    adjustments: list["Adjustment"] = Relationship(
        back_populates="worklog", cascade_delete=True
    )


class TimeSegment(SQLModel, table=True):
    """
    Individual work entry within a worklog.
    Each segment can be independently recorded, questioned, removed, or adjusted.
    """

    __tablename__ = "time_segment"
    __table_args__ = (
        Index("ix_time_segment_worklog_id", "worklog_id"),
        Index("ix_time_segment_segment_date", "segment_date"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    worklog_id: uuid.UUID = Field(foreign_key="worklog.id", nullable=False)
    hours_worked: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Hours worked in this segment",
    )
    hourly_rate: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Hourly rate for this work",
    )
    segment_date: date = Field(
        nullable=False, description="Date when work was performed"
    )
    notes: str | None = Field(default=None, max_length=1000)
    deleted_at: datetime | None = Field(
        default=None, nullable=True, description="Soft delete timestamp for audit trail"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": lambda: datetime.utcnow()},
    )

    # Relationships
    worklog: WorkLog = Relationship(back_populates="time_segments")
    remittance_lines: list["RemittanceLine"] = Relationship(
        back_populates="time_segment"
    )

    @property
    def gross_amount(self) -> Decimal:
        """Calculate gross amount for this time segment."""
        return self.hours_worked * self.hourly_rate


class Adjustment(SQLModel, table=True):
    """
    Retroactive deduction or addition to a worklog.
    Can be applied to previously settled or unsettled work.
    """

    __tablename__ = "adjustment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    worklog_id: uuid.UUID = Field(foreign_key="worklog.id", nullable=False, index=True)
    adjustment_type: AdjustmentType = Field(nullable=False)
    amount: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Adjustment amount (positive value)",
    )
    reason: str = Field(max_length=500, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    # Relationships
    worklog: WorkLog = Relationship(back_populates="adjustments")
    remittance_lines: list["RemittanceLine"] = Relationship(back_populates="adjustment")


class Settlement(SQLModel, table=True):
    """
    Record of a monthly settlement run.
    Groups all remittances generated in a single settlement period.
    """

    __tablename__ = "settlement"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    period_start: date = Field(
        nullable=False, description="Start date of settlement period"
    )
    period_end: date = Field(
        nullable=False, description="End date of settlement period"
    )
    run_at: datetime = Field(
        default_factory=datetime.utcnow,
        nullable=False,
        description="When settlement was executed",
    )
    status: SettlementStatus = Field(nullable=False, default=SettlementStatus.COMPLETED)
    total_remittances_generated: int = Field(default=0, nullable=False)

    # Relationships
    remittances: list["Remittance"] = Relationship(
        back_populates="settlement", cascade_delete=True
    )


class Remittance(SQLModel, table=True):
    """
    Payment record for a worker.
    Represents a single payout attempt with breakdown of amounts.
    """

    __tablename__ = "remittance"
    __table_args__ = (
        Index("ix_remittance_worker_user_id", "worker_user_id"),
        Index("ix_remittance_status", "status"),
        Index("ix_remittance_settlement_id", "settlement_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    settlement_id: uuid.UUID = Field(foreign_key="settlement.id", nullable=False)
    worker_user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False)
    gross_amount: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Total from time segments",
    )
    adjustments_amount: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False, default=0),
        description="Total adjustments (can be negative for deductions)",
    )
    net_amount: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Final amount to be paid",
    )
    status: RemittanceStatus = Field(nullable=False, default=RemittanceStatus.PENDING)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": lambda: datetime.utcnow()},
    )
    paid_at: datetime | None = Field(default=None, nullable=True)

    # Relationships
    settlement: Settlement = Relationship(back_populates="remittances")
    remittance_lines: list["RemittanceLine"] = Relationship(
        back_populates="remittance", cascade_delete=True
    )


class RemittanceLine(SQLModel, table=True):
    """
    Junction table tracking exactly which work was paid in which remittance.
    Provides audit trail and prevents double-payment.
    """

    __tablename__ = "remittance_line"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    remittance_id: uuid.UUID = Field(
        foreign_key="remittance.id", nullable=False, index=True
    )
    time_segment_id: uuid.UUID | None = Field(
        foreign_key="time_segment.id", nullable=True, index=True
    )
    adjustment_id: uuid.UUID | None = Field(
        foreign_key="adjustment.id", nullable=True, index=True
    )
    amount: Decimal = Field(
        sa_column=Column(DECIMAL(10, 2), nullable=False),
        description="Amount attributed to this line item",
    )

    # Relationships
    remittance: Remittance = Relationship(back_populates="remittance_lines")
    time_segment: TimeSegment | None = Relationship(back_populates="remittance_lines")
    adjustment: Adjustment | None = Relationship(back_populates="remittance_lines")
