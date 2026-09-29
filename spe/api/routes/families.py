"""服务端业务模块。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from spe.api.deps import Svc, TenantId
from spe.api.schemas.http import (
    AddMemberRequest,
    AdjustFloorRequest,
    AdjustPoolRequest,
    CreateFamilyRequest,
    FamilyMemberOut,
    FamilyOut,
    FamilyUsageResponse,
    MemberUsageOut,
    UserFamilyUsageResponse,
)
from spe.domain.family import Family, FamilyMember, FamilyUsageView
from spe.domain.reason_codes import ReasonCode
from spe.domain.services.family_service import (
    FamilyError,
    FamilyMemberConflict,
    FamilyVersionConflict,
)

router = APIRouter(prefix="/v1/families", tags=["families"])

_NOT_FOUND = {
    ReasonCode.REJECTED_FAMILY_NOT_FOUND,
    ReasonCode.REJECTED_FAMILY_MEMBER_NOT_FOUND,
}


def _raise_family_error(exc: FamilyError) -> None:
    if exc.reason in _NOT_FOUND:
        code = status.HTTP_404_NOT_FOUND
    elif exc.reason is ReasonCode.REJECTED_FAMILY_FLOOR_EXCEEDS_POOL:
        code = 422
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail={"reason": exc.reason.value})


def _family_out(family: Family) -> FamilyOut:
    return FamilyOut(
        id=family.id,
        tenant_id=family.tenant_id,
        name=family.name,
        timezone=family.timezone,
        daily_pool_seconds=family.daily_pool_seconds,
        version=family.version,
    )


def _member_out(member: FamilyMember) -> FamilyMemberOut:
    return FamilyMemberOut(
        id=member.id,
        tenant_id=member.tenant_id,
        family_id=member.family_id,
        user_id=member.user_id,
        floor_seconds=member.floor_seconds,
        status=member.status.value,
        removed_at=member.removed_at,
    )


def _usage_out(view: FamilyUsageView) -> FamilyUsageResponse:
    return FamilyUsageResponse(
        family=_family_out(view.family),
        local_day=view.local_day,
        pool_used_seconds=view.pool_used_seconds,
        pool_remaining_seconds=view.pool_remaining_seconds,
        members=[
            MemberUsageOut(
                user_id=m.user_id,
                floor_seconds=m.floor_seconds,
                status=m.status.value,
                used_seconds=m.used_seconds,
            )
            for m in view.members
        ],
    )


@router.post("", response_model=FamilyOut, status_code=status.HTTP_201_CREATED)
async def create_family(
    body: CreateFamilyRequest, tenant_id: TenantId, svc: Svc
) -> FamilyOut:
    try:
        family = await svc.family_service.create_family(
            tenant_id=tenant_id,
            name=body.name,
            timezone=body.timezone,
            daily_pool_seconds=body.daily_pool_seconds,
        )
    except FamilyError as exc:
        _raise_family_error(exc)
        raise  # unreachable, keeps type checkers happy
    return _family_out(family)


@router.get("/{family_id}", response_model=FamilyOut)
async def get_family(family_id: str, tenant_id: TenantId, svc: Svc) -> FamilyOut:
    try:
        family = await svc.family_service.get_family(tenant_id, family_id)
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _family_out(family)


@router.post("/{family_id}/adjust", response_model=FamilyOut)
async def adjust_pool(
    family_id: str, body: AdjustPoolRequest, tenant_id: TenantId, svc: Svc
) -> FamilyOut:
    try:
        family = await svc.family_service.adjust_pool(
            tenant_id,
            family_id,
            body.daily_pool_seconds,
            expected_version=body.expected_version,
        )
    except FamilyVersionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": ReasonCode.REJECTED_FAMILY_VERSION_CONFLICT.value},
        ) from exc
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _family_out(family)


@router.get("/{family_id}/usage", response_model=FamilyUsageResponse)
async def family_usage(
    family_id: str, tenant_id: TenantId, svc: Svc
) -> FamilyUsageResponse:
    try:
        view = await svc.family_service.get_usage(tenant_id, family_id)
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _usage_out(view)


@router.get("/users/{user_id}/usage", response_model=UserFamilyUsageResponse)
async def user_family_usage(
    user_id: str, tenant_id: TenantId, svc: Svc
) -> UserFamilyUsageResponse:
    try:
        view, own = await svc.family_service.get_member_usage_by_user(tenant_id, user_id)
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return UserFamilyUsageResponse(
        usage=_usage_out(view),
        member=MemberUsageOut(
            user_id=own.user_id,
            floor_seconds=own.floor_seconds,
            status=own.status.value,
            used_seconds=own.used_seconds,
        ),
    )


@router.post(
    "/{family_id}/members", response_model=FamilyMemberOut, status_code=status.HTTP_201_CREATED
)
async def add_member(
    family_id: str, body: AddMemberRequest, tenant_id: TenantId, svc: Svc
) -> FamilyMemberOut:
    try:
        member = await svc.family_service.add_member(
            tenant_id, family_id, body.user_id, body.floor_seconds
        )
    except FamilyMemberConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": ReasonCode.REJECTED_FAMILY_MEMBER_ALREADY_ACTIVE.value},
        ) from exc
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _member_out(member)


@router.get("/members/{member_id}", response_model=FamilyMemberOut)
async def get_member(member_id: str, tenant_id: TenantId, svc: Svc) -> FamilyMemberOut:
    try:
        member = await svc.family_service.get_member(tenant_id, member_id)
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _member_out(member)


@router.post("/members/{member_id}/floor", response_model=FamilyMemberOut)
async def adjust_floor(
    member_id: str, body: AdjustFloorRequest, tenant_id: TenantId, svc: Svc
) -> FamilyMemberOut:
    try:
        member = await svc.family_service.adjust_member_floor(
            tenant_id, member_id, body.floor_seconds
        )
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _member_out(member)


@router.delete("/members/{member_id}", response_model=FamilyMemberOut)
async def remove_member(member_id: str, tenant_id: TenantId, svc: Svc) -> FamilyMemberOut:
    try:
        member = await svc.family_service.remove_member(tenant_id, member_id)
    except FamilyError as exc:
        _raise_family_error(exc)
        raise
    return _member_out(member)
