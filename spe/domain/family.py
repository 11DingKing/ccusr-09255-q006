"""家庭共享额度领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from spe.domain.reason_codes import ReasonCode


class FamilyMemberStatus(StrEnum):
    """封装领域状态与业务约束。"""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


@dataclass
class Family:
    """一个租户下的家庭共享池；总额度按家庭本地日重置。"""

    id: str
    tenant_id: str
    name: str
    timezone: str
    daily_pool_seconds: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass
class FamilyMember:
    """家庭成员关系；floor_seconds 为个人保底额度（从共享池中预留）。"""

    id: str
    tenant_id: str
    family_id: str
    user_id: str
    floor_seconds: int
    status: FamilyMemberStatus
    created_at: datetime
    updated_at: datetime
    removed_at: datetime | None = None


@dataclass
class MemberUsageView:
    """封装领域状态与业务约束。"""

    user_id: str
    floor_seconds: int
    status: FamilyMemberStatus
    used_seconds: int


@dataclass
class FamilyUsageView:
    """封装领域状态与业务约束。"""

    family: Family
    local_day: str
    pool_used_seconds: int
    pool_remaining_seconds: int
    members: list[MemberUsageView] = field(default_factory=list)


class FamilyError(Exception):
    """家庭业务校验失败；reason 用于 API 响应。"""

    def __init__(self, reason: ReasonCode, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason.value)
