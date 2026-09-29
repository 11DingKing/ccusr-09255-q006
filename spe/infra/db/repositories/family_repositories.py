"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from spe.domain.family import Family, FamilyMember, FamilyMemberStatus
from spe.domain.services.family_service import (
    FamilyAlreadyExists,
    FamilyMemberConflict,
    FamilyVersionConflict,
)
from spe.infra.db.models import (
    FamilyMemberModel,
    FamilyMemberUsageModel,
    FamilyModel,
    FamilyPoolLedgerModel,
)


def family_to_domain(model: FamilyModel) -> Family:
    return Family(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        timezone=model.timezone,
        daily_pool_seconds=model.daily_pool_seconds,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def member_to_domain(model: FamilyMemberModel) -> FamilyMember:
    return FamilyMember(
        id=model.id,
        tenant_id=model.tenant_id,
        family_id=model.family_id,
        user_id=model.user_id,
        floor_seconds=model.floor_seconds,
        status=FamilyMemberStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
        removed_at=model.removed_at,
    )


class SqlFamilyRepository:
    """家庭关系与共享池账本的 SQLite 实现。"""

    def __init__(self, db: AsyncSession, clock_now: datetime) -> None:
        self._db = db
        self._now = clock_now

    # -- family aggregate ----------------------------------------------------

    async def add_family(self, family: Family) -> None:
        self._db.add(
            FamilyModel(
                id=family.id,
                tenant_id=family.tenant_id,
                name=family.name,
                timezone=family.timezone,
                daily_pool_seconds=family.daily_pool_seconds,
                version=family.version,
                created_at=family.created_at,
                updated_at=family.updated_at,
            )
        )
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise FamilyAlreadyExists(str(exc)) from exc

    async def save_family(self, family: Family) -> None:
        stmt = select(FamilyModel).where(
            FamilyModel.tenant_id == family.tenant_id, FamilyModel.id == family.id
        )
        model = (await self._db.execute(stmt)).scalar_one()
        if model.version != family.version:
            raise FamilyVersionConflict(
                f"expected version {model.version}, got {family.version}"
            )
        model.name = family.name
        model.timezone = family.timezone
        model.daily_pool_seconds = family.daily_pool_seconds
        model.version = family.version + 1
        model.updated_at = family.updated_at
        await self._db.flush()
        family.version = model.version

    async def get_family(self, tenant_id: str, family_id: str) -> Family | None:
        stmt = select(FamilyModel).where(
            FamilyModel.tenant_id == tenant_id, FamilyModel.id == family_id
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return family_to_domain(model) if model else None

    # -- memberships ---------------------------------------------------------

    async def add_member(self, member: FamilyMember) -> None:
        self._db.add(
            FamilyMemberModel(
                id=member.id,
                tenant_id=member.tenant_id,
                family_id=member.family_id,
                user_id=member.user_id,
                floor_seconds=member.floor_seconds,
                status=member.status.value,
                created_at=member.created_at,
                updated_at=member.updated_at,
                removed_at=member.removed_at,
            )
        )
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise FamilyMemberConflict(str(exc)) from exc

    async def save_member(self, member: FamilyMember) -> None:
        stmt = select(FamilyMemberModel).where(
            FamilyMemberModel.tenant_id == member.tenant_id,
            FamilyMemberModel.id == member.id,
        )
        model = (await self._db.execute(stmt)).scalar_one()
        model.floor_seconds = member.floor_seconds
        model.status = member.status.value
        model.removed_at = member.removed_at
        model.updated_at = member.updated_at
        try:
            await self._db.flush()
        except IntegrityError as exc:
            # 状态为 ACTIVE 的部分唯一索引可能在重新激活时冲突。
            await self._db.rollback()
            raise FamilyMemberConflict(str(exc)) from exc

    async def get_member(self, tenant_id: str, member_id: str) -> FamilyMember | None:
        stmt = select(FamilyMemberModel).where(
            FamilyMemberModel.tenant_id == tenant_id, FamilyMemberModel.id == member_id
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return member_to_domain(model) if model else None

    async def get_active_member_by_user(
        self, tenant_id: str, user_id: str
    ) -> FamilyMember | None:
        stmt = select(FamilyMemberModel).where(
            FamilyMemberModel.tenant_id == tenant_id,
            FamilyMemberModel.user_id == user_id,
            FamilyMemberModel.status == FamilyMemberStatus.ACTIVE.value,
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return member_to_domain(model) if model else None

    async def list_members(
        self, tenant_id: str, family_id: str, *, include_removed: bool = False
    ) -> list[FamilyMember]:
        stmt = select(FamilyMemberModel).where(
            FamilyMemberModel.tenant_id == tenant_id,
            FamilyMemberModel.family_id == family_id,
        )
        if not include_removed:
            stmt = stmt.where(
                FamilyMemberModel.status == FamilyMemberStatus.ACTIVE.value
            )
        rows = (await self._db.execute(stmt.order_by(FamilyMemberModel.created_at))).scalars()
        return [member_to_domain(m) for m in rows]

    # -- pool ledger ---------------------------------------------------------

    async def pool_used(
        self, tenant_id: str, family_id: str, local_day: str
    ) -> int:
        stmt = select(FamilyPoolLedgerModel.seconds).where(
            FamilyPoolLedgerModel.tenant_id == tenant_id,
            FamilyPoolLedgerModel.family_id == family_id,
            FamilyPoolLedgerModel.local_day == local_day,
        )
        return (await self._db.execute(stmt)).scalar() or 0

    async def member_used(
        self, tenant_id: str, family_id: str, user_id: str, local_day: str
    ) -> int:
        stmt = select(FamilyMemberUsageModel.seconds).where(
            FamilyMemberUsageModel.tenant_id == tenant_id,
            FamilyMemberUsageModel.family_id == family_id,
            FamilyMemberUsageModel.user_id == user_id,
            FamilyMemberUsageModel.local_day == local_day,
        )
        return (await self._db.execute(stmt)).scalar() or 0

    async def pool_usage_by_member(
        self, tenant_id: str, family_id: str, local_day: str
    ) -> dict[str, int]:
        stmt = select(
            FamilyMemberUsageModel.user_id, FamilyMemberUsageModel.seconds
        ).where(
            FamilyMemberUsageModel.tenant_id == tenant_id,
            FamilyMemberUsageModel.family_id == family_id,
            FamilyMemberUsageModel.local_day == local_day,
        )
        return {user_id: seconds for user_id, seconds in await self._db.execute(stmt)}

    async def charge_pool(
        self,
        tenant_id: str,
        family_id: str,
        local_day: str,
        seconds: int,
        cap_seconds: int,
    ) -> int:
        if seconds <= 0:
            return 1
        stmt = sqlite_insert(FamilyPoolLedgerModel).values(
            tenant_id=tenant_id,
            family_id=family_id,
            local_day=local_day,
            seconds=seconds,
        ).on_conflict_do_update(
            index_elements=["tenant_id", "family_id", "local_day"],
            set_={"seconds": FamilyPoolLedgerModel.seconds + seconds},
            # 条件原子守卫：即使并发会话同时到达，也绝不允许共享池透支。
            where=FamilyPoolLedgerModel.seconds + seconds <= cap_seconds,
        )
        result = await self._db.execute(stmt)
        return result.rowcount

    async def add_member_usage(
        self,
        tenant_id: str,
        family_id: str,
        user_id: str,
        local_day: str,
        seconds: int,
    ) -> None:
        if seconds <= 0:
            return
        stmt = sqlite_insert(FamilyMemberUsageModel).values(
            tenant_id=tenant_id,
            family_id=family_id,
            user_id=user_id,
            local_day=local_day,
            seconds=seconds,
        ).on_conflict_do_update(
            index_elements=["tenant_id", "family_id", "user_id", "local_day"],
            set_={"seconds": FamilyMemberUsageModel.seconds + seconds},
        )
        await self._db.execute(stmt)
