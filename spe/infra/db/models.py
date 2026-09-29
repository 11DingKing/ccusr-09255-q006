"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from spe.infra.db.base import Base

# BigInteger autoincrement primary keys need to render as plain INTEGER on
# SQLite (only INTEGER PRIMARY KEY is an alias for the auto-incrementing rowid),
# 在 SQLite 中以整数保存累计秒数。
AutoBigInt = BigInteger().with_variant(Integer, "sqlite")


class PolicyModel(Base):
    """封装领域状态与业务约束。"""

    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_policies_tenant_version"),
        Index("ix_policies_tenant_active", "tenant_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionModel(Base):
    """封装领域状态与业务约束。"""

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_sessions_tenant_idem"
        ),
        # The single-active-session guard is a partial unique index created in
        # 由 SQLite 过滤索引保证。此处声明为
        # a plain index for ORM metadata; the real uniqueness is added by Alembic.
        Index("ix_sessions_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    watched_seconds_marker: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_watched_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DailyUsageLedgerModel(Base):
    """封装领域状态与业务约束。"""

    __tablename__ = "daily_usage_ledger"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "local_day", name="uq_ledger_tenant_user_day"
        ),
    )

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class HeartbeatModel(Base):
    """封装领域状态与业务约束。"""

    __tablename__ = "heartbeats"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_heartbeat_session_seq"),
    )

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    watched_seconds_total: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxModel(Base):
    """封装领域状态与业务约束。"""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_unpublished", "published", "id"),)

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FamilyModel(Base):
    """家庭共享池配置；版本号用于并发额度调整的乐观并发控制。"""

    __tablename__ = "families"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_families_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_pool_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FamilyMemberModel(Base):
    """家庭成员关系；移除只改状态，已用额度不回退。"""

    __tablename__ = "family_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_family_members_tenant_id"),
        # 每用户在同一租户下只能有一个 ACTIVE 家庭关系，由部分唯一索引保证
        # （与 sessions 的单活动会话索引同样在 ddl.py 中创建）。
        Index("ix_family_members_tenant_user", "tenant_id", "user_id"),
        Index("ix_family_members_family", "tenant_id", "family_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("families.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    floor_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FamilyPoolLedgerModel(Base):
    """家庭共享池每日已用量（自然日按家庭时区切分，跨日自动归零）。"""

    __tablename__ = "family_pool_ledger"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "family_id", "local_day", name="uq_family_pool_tenant_family_day"
        ),
    )

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FamilyMemberUsageModel(Base):
    """成员维度的家庭池已用量，用于用量查询与保底计算。"""

    __tablename__ = "family_member_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "family_id",
            "user_id",
            "local_day",
            name="uq_family_member_usage_day",
        ),
    )

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
