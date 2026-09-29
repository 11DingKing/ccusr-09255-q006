"""家庭共享额度领域服务。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from spe.domain.clock import Clock
from spe.domain.events import DomainEvent
from spe.domain.family import (
    Family,
    FamilyError,
    FamilyMember,
    FamilyMemberStatus,
    FamilyUsageView,
    MemberUsageView,
)
from spe.domain.ids import IdGenerator
from spe.domain.reason_codes import ReasonCode
from spe.domain.repositories import FamilyRepository, OutboxRepository
from spe.domain.timeutil import local_day_key


class FamilyAlreadyExists(Exception):
    """家庭主键冲突（理论上不应发生，ID 由生成器产出）。"""


class FamilyMemberConflict(Exception):
    """用户已存在 ACTIVE 家庭关系。"""


class FamilyVersionConflict(Exception):
    """家庭总额度调整基于过期版本。"""


class FamilyService:
    """维护家庭关系、共享池总额与个人保底，并提供用量查询。"""

    def __init__(
        self,
        families: FamilyRepository,
        outbox: OutboxRepository,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._families = families
        self._outbox = outbox
        self._clock = clock
        self._ids = ids

    # -- family aggregate ----------------------------------------------------

    async def create_family(
        self,
        tenant_id: str,
        name: str,
        timezone: str,
        daily_pool_seconds: int,
    ) -> Family:
        if not name.strip():
            raise FamilyError(ReasonCode.REJECTED_POLICY_INVALID, "family name is empty")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise FamilyError(
                ReasonCode.REJECTED_POLICY_INVALID, f"unknown timezone: {timezone!r}"
            ) from exc
        if daily_pool_seconds <= 0:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL,
                "daily pool must be positive",
            )
        now = self._clock.now()
        family = Family(
            id=self._ids.new_id(),
            tenant_id=tenant_id,
            name=name.strip(),
            timezone=timezone,
            daily_pool_seconds=daily_pool_seconds,
            version=1,
            created_at=now,
            updated_at=now,
        )
        await self._families.add_family(family)
        await self._emit("family.created", tenant_id, family.id, now, pool=daily_pool_seconds)
        return family

    async def adjust_pool(
        self,
        tenant_id: str,
        family_id: str,
        daily_pool_seconds: int,
        expected_version: int | None = None,
    ) -> Family:
        family = await self._require_family(tenant_id, family_id)
        if expected_version is not None and expected_version != family.version:
            raise FamilyVersionConflict(
                f"expected {expected_version}, current {family.version}"
            )
        if daily_pool_seconds <= 0:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL,
                "daily pool must be positive",
            )
        floors = sum(
            m.floor_seconds
            for m in await self._families.list_members(tenant_id, family_id)
        )
        if floors > daily_pool_seconds:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL,
                f"active floors total {floors}s exceeds new pool {daily_pool_seconds}s",
            )
        family.daily_pool_seconds = daily_pool_seconds
        family.updated_at = self._clock.now()
        await self._families.save_family(family)
        await self._emit(
            "family.pool_adjusted",
            tenant_id,
            family.id,
            family.updated_at,
            pool=daily_pool_seconds,
            version=family.version,
        )
        return family

    # -- memberships ---------------------------------------------------------

    async def add_member(
        self,
        tenant_id: str,
        family_id: str,
        user_id: str,
        floor_seconds: int,
    ) -> FamilyMember:
        family = await self._require_family(tenant_id, family_id)
        if not user_id.strip():
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND, "empty user_id")
        if floor_seconds < 0:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL, "floor must be >= 0"
            )
        existing = await self._families.get_active_member_by_user(tenant_id, user_id)
        if existing is not None:
            raise FamilyMemberConflict(user_id)
        floors = sum(
            m.floor_seconds
            for m in await self._families.list_members(tenant_id, family_id)
        )
        if floors + floor_seconds > family.daily_pool_seconds:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL,
                f"floors total {floors + floor_seconds}s exceeds pool "
                f"{family.daily_pool_seconds}s",
            )
        now = self._clock.now()
        member = FamilyMember(
            id=self._ids.new_id(),
            tenant_id=tenant_id,
            family_id=family_id,
            user_id=user_id.strip(),
            floor_seconds=floor_seconds,
            status=FamilyMemberStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        try:
            await self._families.add_member(member)
        except FamilyMemberConflict as exc:
            raise FamilyMemberConflict(user_id) from exc
        await self._emit(
            "family.member_added",
            tenant_id,
            family.id,
            now,
            member_id=member.id,
            user_id=member.user_id,
            floor_seconds=floor_seconds,
        )
        return member

    async def remove_member(self, tenant_id: str, member_id: str) -> FamilyMember:
        """移除成员：关系置为 REMOVED，当日已用额度不回退到共享池。"""
        member = await self._families.get_member(tenant_id, member_id)
        if member is None:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND)
        if member.status is FamilyMemberStatus.REMOVED:
            return member
        member.status = FamilyMemberStatus.REMOVED
        now = self._clock.now()
        member.removed_at = now
        member.updated_at = now
        await self._families.save_member(member)
        await self._emit(
            "family.member_removed",
            tenant_id,
            member.family_id,
            now,
            member_id=member.id,
            user_id=member.user_id,
        )
        return member

    async def adjust_member_floor(
        self, tenant_id: str, member_id: str, floor_seconds: int
    ) -> FamilyMember:
        member = await self._families.get_member(tenant_id, member_id)
        if member is None:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND)
        if member.status is not FamilyMemberStatus.ACTIVE:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND)
        if floor_seconds < 0:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL, "floor must be >= 0"
            )
        family = await self._require_family(tenant_id, member.family_id)
        floors = sum(
            m.floor_seconds
            for m in await self._families.list_members(tenant_id, family.id)
        )
        if floors - member.floor_seconds + floor_seconds > family.daily_pool_seconds:
            raise FamilyError(
                ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL,
                "new floor total would exceed the family pool",
            )
        member.floor_seconds = floor_seconds
        member.updated_at = self._clock.now()
        await self._families.save_member(member)
        await self._emit(
            "family.member_floor_adjusted",
            tenant_id,
            family.id,
            member.updated_at,
            member_id=member.id,
            floor_seconds=floor_seconds,
        )
        return member

    # -- queries -------------------------------------------------------------

    async def get_family(self, tenant_id: str, family_id: str) -> Family:
        return await self._require_family(tenant_id, family_id)

    async def get_member(self, tenant_id: str, member_id: str) -> FamilyMember:
        member = await self._families.get_member(tenant_id, member_id)
        if member is None:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND)
        return member

    async def get_usage(
        self, tenant_id: str, family_id: str, *, local_day: str | None = None
    ) -> FamilyUsageView:
        family = await self._require_family(tenant_id, family_id)
        day = local_day or local_day_key(self._clock.now(), family.timezone)
        pool_used = await self._families.pool_used(tenant_id, family_id, day)
        by_member = await self._families.pool_usage_by_member(tenant_id, family_id, day)
        members = await self._families.list_members(
            tenant_id, family_id, include_removed=True
        )
        views = [
            MemberUsageView(
                user_id=m.user_id,
                floor_seconds=m.floor_seconds if m.status is FamilyMemberStatus.ACTIVE else 0,
                status=m.status,
                used_seconds=by_member.get(m.user_id, 0),
            )
            for m in members
        ]
        return FamilyUsageView(
            family=family,
            local_day=day,
            pool_used_seconds=pool_used,
            pool_remaining_seconds=max(0, family.daily_pool_seconds - pool_used),
            members=views,
        )

    async def get_member_usage_by_user(
        self, tenant_id: str, user_id: str
    ) -> tuple[FamilyUsageView, MemberUsageView]:
        member = await self._families.get_active_member_by_user(tenant_id, user_id)
        if member is None:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND)
        view = await self.get_usage(tenant_id, member.family_id)
        own = next((m for m in view.members if m.user_id == user_id), None)
        assert own is not None
        return view, own

    # -- helpers -------------------------------------------------------------

    async def _require_family(self, tenant_id: str, family_id: str) -> Family:
        family = await self._families.get_family(tenant_id, family_id)
        if family is None:
            raise FamilyError(ReasonCode.REJECTED_FAMILY_NOT_FOUND)
        return family

    async def _emit(
        self,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        now: datetime,
        **payload: object,
    ) -> None:
        await self._outbox.add(
            DomainEvent(
                event_type=event_type,
                tenant_id=tenant_id,
                aggregate_id=aggregate_id,
                occurred_at=now,
                payload=payload,
            )
        )
