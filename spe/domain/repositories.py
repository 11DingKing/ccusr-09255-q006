"""服务端业务模块。"""

from __future__ import annotations

from typing import Protocol

from spe.domain.events import DomainEvent
from spe.domain.family import Family, FamilyMember
from spe.domain.policy_ast import PolicyDocument
from spe.domain.session import Session


class PolicyRecord(Protocol):
    """封装领域状态与业务约束。"""

    @property
    def id(self) -> str: ...
    @property
    def tenant_id(self) -> str: ...
    @property
    def version(self) -> int: ...
    @property
    def document(self) -> PolicyDocument: ...


class PolicyRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def next_version(self, tenant_id: str) -> int:
        """执行确定性的业务处理。"""
        ...

    async def add(
        self, tenant_id: str, version: int, document: PolicyDocument, policy_id: str
    ) -> PolicyRecord:
        """执行确定性的业务处理。"""
        ...

    async def get_active(self, tenant_id: str) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...

    async def get_version(self, tenant_id: str, version: int) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...

    async def get_by_id(self, tenant_id: str, policy_id: str) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...


class SessionRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def add(self, session: Session) -> None: ...

    async def get(self, tenant_id: str, session_id: str) -> Session | None: ...

    async def get_for_update(
        self, tenant_id: str, session_id: str
    ) -> Session | None:
        """执行确定性的业务处理。"""
        ...

    async def get_active_for_user(self, tenant_id: str, user_id: str) -> Session | None: ...

    async def get_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> Session | None: ...

    async def save(self, session: Session, idempotency_key: str | None = None) -> None: ...


class DailyUsageLedger(Protocol):
    """封装领域状态与业务约束。"""

    async def get_seconds(self, tenant_id: str, user_id: str, local_day: str) -> int:
        """执行确定性的业务处理。"""
        ...

    async def add_seconds(
        self, tenant_id: str, user_id: str, local_day: str, seconds: int
    ) -> int:
        """执行确定性的业务处理。"""
        ...


class FamilyRepository(Protocol):
    """家庭关系与共享池账本存储。"""

    # -- family aggregate ----------------------------------------------------
    async def add_family(self, family: Family) -> None: ...

    async def save_family(self, family: Family) -> None: ...

    async def get_family(self, tenant_id: str, family_id: str) -> Family | None: ...

    # -- memberships ---------------------------------------------------------
    async def add_member(self, member: FamilyMember) -> None: ...

    async def save_member(self, member: FamilyMember) -> None: ...

    async def get_member(self, tenant_id: str, member_id: str) -> FamilyMember | None: ...

    async def get_active_member_by_user(
        self, tenant_id: str, user_id: str
    ) -> FamilyMember | None: ...

    async def list_members(
        self, tenant_id: str, family_id: str, *, include_removed: bool = False
    ) -> list[FamilyMember]: ...

    # -- pool ledger ---------------------------------------------------------
    async def pool_used(
        self, tenant_id: str, family_id: str, local_day: str
    ) -> int: ...

    async def member_used(
        self, tenant_id: str, family_id: str, user_id: str, local_day: str
    ) -> int: ...

    async def pool_usage_by_member(
        self, tenant_id: str, family_id: str, local_day: str
    ) -> dict[str, int]:
        """返回 user_id -> 当日已从共享池扣减的秒数。"""
        ...

    async def charge_pool(
        self,
        tenant_id: str,
        family_id: str,
        local_day: str,
        seconds: int,
        cap_seconds: int,
    ) -> int:
        """条件原子扣减共享池。

        仅当 ``当前已用 + seconds <= cap_seconds`` 时累加并返回 1；
        否则不写入并返回 0。调用方可在同一事务内重读后缩小秒数重试。
        """
        ...

    async def add_member_usage(
        self,
        tenant_id: str,
        family_id: str,
        user_id: str,
        local_day: str,
        seconds: int,
    ) -> None: ...


class OutboxRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def add(self, event: DomainEvent) -> None: ...


class HeartbeatSink(Protocol):
    """封装领域状态与业务约束。"""

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        seq: int,
        watched_seconds_total: int,
        credited_seconds: int,
        occurred_at: object,
    ) -> None: ...
