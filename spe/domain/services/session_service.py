"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from spe.domain.clock import Clock
from spe.domain.events import DomainEvent
from spe.domain.family import Family, FamilyMember
from spe.domain.ids import IdGenerator
from spe.domain.policy_ast import PolicyDocument
from spe.domain.policy_interpreter import EvalContext, evaluate
from spe.domain.reason_codes import ReasonCode
from spe.domain.repositories import (
    DailyUsageLedger,
    FamilyRepository,
    HeartbeatSink,
    OutboxRepository,
    PolicyRepository,
    SessionRepository,
)
from spe.domain.services.responses import ActionResult
from spe.domain.session import Session, SessionStatus
from spe.domain.timeutil import age_at, local_day_key, split_watch_window, split_watch_window_multi


class ActiveSessionExists(Exception):
    """封装领域状态与业务约束。"""


@dataclass
class _FamilyDayState:
    """单个家庭本地日内的共享池快照与本心跳内的待提交增量。"""

    family: Family
    member: FamilyMember
    pool_used: int
    by_member: dict[str, int]
    floors: dict[str, int]

    def others_reserved(self) -> int:
        """其他活跃成员尚未用满的保底额度之和（扣减时必须预留）。"""
        total = 0
        for user_id, floor in self.floors.items():
            if user_id == self.member.user_id:
                continue
            total += max(0, floor - self.by_member.get(user_id, 0))
        return total


def _family_day_map(per_family_day: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    """把 (family_id, day) -> 秒 转换为可 JSON 序列化的嵌套字典。"""
    out: dict[str, dict[str, int]] = {}
    for (family_id, day), seconds in per_family_day.items():
        out.setdefault(family_id, {})[day] = seconds
    return out


class SessionService:
    """封装领域状态与业务约束。"""

    def __init__(
        self,
        sessions: SessionRepository,
        policies: PolicyRepository,
        outbox: OutboxRepository,
        clock: Clock,
        ids: IdGenerator,
        ledger: DailyUsageLedger,
        heartbeats: HeartbeatSink | None = None,
        heartbeat_max_gap_seconds: int = 90,
        families: FamilyRepository | None = None,
    ) -> None:
        self._sessions = sessions
        self._policies = policies
        self._outbox = outbox
        self._clock = clock
        self._ids = ids
        self._ledger = ledger
        self._heartbeats = heartbeats
        self._max_gap = heartbeat_max_gap_seconds
        self._families = families

    # -- start ---------------------------------------------------------------

    async def start(
        self,
        tenant_id: str,
        user_id: str,
        birth_date: date,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        """执行确定性的业务处理。"""
        if idempotency_key is not None:
            existing = await self._sessions.get_by_idempotency_key(tenant_id, idempotency_key)
            if existing is not None:
                return ActionResult.success(
                    ReasonCode.SESSION_STARTED_IDEMPOTENT, session=existing
                )

        policy = await self._policies.get_active(tenant_id)
        if policy is None:
            return ActionResult.rejected(ReasonCode.REJECTED_POLICY_NOT_FOUND)

        now = self._clock.now()
        tz = policy.document.rules.timezone
        # Evaluate eligibility at start against the authoritative daily ledger
        # (age + bedtime + already-consumed daily budget; no session usage yet).
        local_day = local_day_key(now, tz)
        daily_used = await self._ledger.get_seconds(tenant_id, user_id, local_day)
        ctx = EvalContext(
            now=now,
            user_id=user_id,
            user_age=age_at(birth_date, now, tz),
            daily_usage_seconds=daily_used,
            session_elapsed_seconds=0,
        )
        decision = evaluate(policy.document, ctx)
        if not decision.allowed:
            return ActionResult.rejected(decision.reason, trace=decision.trace)

        # 家庭共享池：若该成员今日可再支取的家庭额度已为 0，直接拒绝启动。
        family_blocker = await self._family_pool_blocker(tenant_id, user_id, now, tz)
        if family_blocker is not None:
            return ActionResult.rejected(family_blocker, trace=decision.trace)

        session = Session(
            id=self._ids.new_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            policy_id=policy.id,
            policy_version=policy.version,
            status=SessionStatus.ACTIVE,
            birth_date=birth_date,
            started_at=now,
            updated_at=now,
        )
        try:
            await self._sessions.add(session, idempotency_key=idempotency_key)
        except ActiveSessionExists:
            # Lost a concurrent race: another start committed first.
            current = await self._sessions.get_active_for_user(tenant_id, user_id)
            return ActionResult.rejected(
                ReasonCode.REJECTED_ACTIVE_SESSION_EXISTS, session=current
            )

        await self._emit("session.started", session, now, version=session.policy_version)
        return ActionResult.success(
            ReasonCode.SESSION_STARTED, session=session, trace=decision.trace
        )

    # -- heartbeat -----------------------------------------------------------

    async def heartbeat(
        self,
        tenant_id: str,
        session_id: str,
        seq: int,
        watched_seconds_total: int,
    ) -> ActionResult:
        """执行确定性的业务处理。"""
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.ACTIVE:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_ACTIVE, session=session)

        # Idempotent / ordered guard.
        if seq <= session.last_seq:
            return ActionResult.rejected(
                ReasonCode.HEARTBEAT_IGNORED_STALE,
                session=session,
                received_seq=seq,
                last_seq=session.last_seq,
            )

        now = self._clock.now()
        policy = await self._policies.get_by_id(tenant_id, session.policy_id)
        assert policy is not None  # pinned policy must exist
        tz = policy.document.rules.timezone

        proposed = max(
            0, min(watched_seconds_total - session.watched_seconds_marker, self._max_gap)
        )

        credited, per_day, per_family_day, hit_limit, limit_reason = await self._credit(
            session, policy.document, tz, now, proposed
        )

        # Advance the marker/seq regardless of how much was creditable, so the
        # cumulative accounting stays monotonic and idempotent.
        session.watched_seconds_marker = max(session.watched_seconds_marker, watched_seconds_total)
        session.last_seq = seq
        session.updated_at = now

        if self._heartbeats is not None:
            await self._heartbeats.record(
                tenant_id=tenant_id,
                session_id=session_id,
                seq=seq,
                watched_seconds_total=watched_seconds_total,
                credited_seconds=credited,
                occurred_at=now,
            )

        if hit_limit:
            session.status = SessionStatus.ENDED
            session.ended_at = now
            await self._sessions.save(session)
            await self._emit("session.ended", session, now, cause=limit_reason)
            return ActionResult.rejected(
                ReasonCode.SESSION_ENDED_BY_LIMIT,
                session=session,
                credited_seconds=credited,
                per_day=per_day,
                per_family_day=_family_day_map(per_family_day),
                limit_reason=limit_reason,
            )

        await self._sessions.save(session)
        await self._emit("session.heartbeat", session, now, credited_seconds=credited)
        return ActionResult.success(
            ReasonCode.HEARTBEAT_APPLIED,
            session=session,
            credited_seconds=credited,
            per_day=per_day,
            per_family_day=_family_day_map(per_family_day),
        )

    async def _credit(
        self,
        session: Session,
        document: PolicyDocument,
        timezone: str,
        now: datetime,
        proposed: int,
    ) -> tuple[int, dict[str, int], dict[tuple[str, str], int], bool, str | None]:
        """结算一次心跳：原子扣减个人日额度与家庭共享池。

        返回 (credited, per_personal_day, per_family_day, hit_limit, reason)。
        ``per_family_day`` 的键为 (family_id, local_day)。
        """
        session_cap = (
            document.rules.session_limit.max_seconds
            if document.rules.session_limit
            else None
        )
        daily_cap = (
            document.rules.daily_limit.max_seconds if document.rules.daily_limit else None
        )

        # 解析当前活跃家庭关系；成员被移除后查不到关系，扣减自动退回纯个人模式，
        # 其此前已占用的共享池额度保留不回退。
        family_member = (
            await self._families.get_active_member_by_user(
                session.tenant_id, session.user_id
            )
            if self._families is not None
            else None
        )
        family = None
        if family_member is not None:
            family = await self._families.get_family(
                session.tenant_id, family_member.family_id
            )
            if family is None:
                # 防御：关系存在但家庭不存在时按无家庭处理。
                family_member = None

        if family_member is None or family is None:
            credited, per_day, hit_limit, limit_reason = await self._credit_personal_only(
                session, timezone, now, proposed, session_cap, daily_cap
            )
            return credited, per_day, {}, hit_limit, limit_reason

        return await self._credit_with_family(
            session,
            family,
            family_member,
            timezone,
            now,
            proposed,
            session_cap,
            daily_cap,
        )

    async def _credit_personal_only(
        self,
        session: Session,
        timezone: str,
        now: datetime,
        proposed: int,
        session_cap: int | None,
        daily_cap: int | None,
    ) -> tuple[int, dict[str, int], bool, str | None]:
        """无家庭关系时的原有结算路径（仅个人单次/日额度）。"""
        credited = 0
        per_day: dict[str, int] = {}
        hit_limit = False
        limit_reason: str | None = None

        for day, segment_seconds in split_watch_window(now, proposed, timezone):
            allow = segment_seconds

            # Bound by remaining session budget.
            if session_cap is not None:
                session_remaining = session_cap - session.total_watched_seconds
                if allow >= session_remaining:
                    allow = max(0, session_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_SESSION_LIMIT_REACHED.value

            # Bound by remaining daily budget for this specific local day.
            if daily_cap is not None:
                already = await self._ledger.get_seconds(session.tenant_id, session.user_id, day)
                day_remaining = daily_cap - (already + per_day.get(day, 0))
                if allow >= day_remaining:
                    allow = max(0, day_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_DAILY_LIMIT_REACHED.value

            if allow > 0:
                await self._ledger.add_seconds(session.tenant_id, session.user_id, day, allow)
                session.total_watched_seconds += allow
                credited += allow
                per_day[day] = per_day.get(day, 0) + allow

            if hit_limit:
                break

        return credited, per_day, hit_limit, limit_reason

    async def _credit_with_family(
        self,
        session: Session,
        family: Family,
        member: FamilyMember,
        policy_tz: str,
        now: datetime,
        proposed: int,
        session_cap: int | None,
        daily_cap: int | None,
    ) -> tuple[int, dict[str, int], dict[tuple[str, str], int], bool, str | None]:
        """家庭路径：同一秒数同时受个人额度与共享池约束，账本在一个事务内落库。"""
        assert self._families is not None
        credited = 0
        per_day: dict[str, int] = {}
        per_family_day: dict[tuple[str, str], int] = {}
        hit_limit = False
        limit_reason: str | None = None

        # family_local_day -> 本次心跳内已计算的共享池状态与待提交增量。
        states: dict[str, _FamilyDayState] = {}

        async def day_state(family_day: str) -> _FamilyDayState:
            state = states.get(family_day)
            if state is None:
                active = await self._families.list_members(
                    session.tenant_id, family.id
                )
                floors = {m.user_id: m.floor_seconds for m in active}
                state = _FamilyDayState(
                    family=family,
                    member=member,
                    pool_used=await self._families.pool_used(
                        session.tenant_id, family.id, family_day
                    ),
                    by_member=await self._families.pool_usage_by_member(
                        session.tenant_id, family.id, family_day
                    ),
                    floors=floors,
                )
                states[family_day] = state
            return state

        for segment_seconds, (personal_day, family_day) in split_watch_window_multi(
            now, proposed, [policy_tz, family.timezone]
        ):
            allow = segment_seconds

            # 1) 个人单次额度。
            if session_cap is not None:
                session_remaining = session_cap - session.total_watched_seconds
                if allow >= session_remaining:
                    allow = max(0, session_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_SESSION_LIMIT_REACHED.value

            # 2) 个人日额度。
            if daily_cap is not None:
                already = await self._ledger.get_seconds(
                    session.tenant_id, session.user_id, personal_day
                )
                day_remaining = daily_cap - (already + per_day.get(personal_day, 0))
                if allow >= day_remaining:
                    allow = max(0, day_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_DAILY_LIMIT_REACHED.value

            # 3) 家庭共享池（含其他成员保底预留）。
            state = await day_state(family_day)
            # 本心跳此前片段已为同一家庭日本地累计的秒数。
            pending_pool = per_family_day.get((family.id, family_day), 0)
            # 可再授予本成员的秒数：总额 - 库内已用 - 本心跳已记 - 他人保底预留。
            family_remaining = (
                family.daily_pool_seconds
                - state.pool_used
                - pending_pool
                - state.others_reserved()
            )
            if allow >= family_remaining:
                allow = max(0, family_remaining)
                hit_limit = True
                limit_reason = ReasonCode.DENIED_FAMILY_POOL_REACHED.value

            if allow > 0:
                # 条件 SQL 守卫为最后一道防线：并发下绝不允许共享池超过总额。
                charged = await self._families.charge_pool(
                    session.tenant_id,
                    family.id,
                    family_day,
                    allow,
                    family.daily_pool_seconds,
                )
                if charged != 1:
                    # 串行事务下守卫通常不会拒绝（Python 侧已按同一总额计算）；
                    # 若被拒绝则重读真实已用量，把本段压缩到剩余额度后重试一次。
                    state.pool_used = await self._families.pool_used(
                        session.tenant_id, family.id, family_day
                    )
                    family_remaining = (
                        family.daily_pool_seconds
                        - state.pool_used
                        - pending_pool
                        - state.others_reserved()
                    )
                    allow = max(0, min(allow, family_remaining))
                    if allow > 0:
                        charged = await self._families.charge_pool(
                            session.tenant_id,
                            family.id,
                            family_day,
                            allow,
                            family.daily_pool_seconds,
                        )
                    if charged != 1:
                        allow = 0
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_FAMILY_POOL_REACHED.value

            if allow > 0:
                # 个人账本与家庭账本在同一数据库事务内一起写入、一起提交。
                await self._ledger.add_seconds(
                    session.tenant_id, session.user_id, personal_day, allow
                )
                await self._families.add_member_usage(
                    session.tenant_id,
                    family.id,
                    session.user_id,
                    family_day,
                    allow,
                )
                session.total_watched_seconds += allow
                credited += allow
                per_day[personal_day] = per_day.get(personal_day, 0) + allow
                per_family_day[(family.id, family_day)] = pending_pool + allow

            if hit_limit:
                break

        return credited, per_day, per_family_day, hit_limit, limit_reason

    # -- pause / resume ------------------------------------------------------

    async def pause(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.ACTIVE:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_ACTIVE, session=session)

        now = self._clock.now()
        session.status = SessionStatus.PAUSED
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.paused", session, now)
        return ActionResult.success(ReasonCode.SESSION_PAUSED, session=session)

    async def resume(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.PAUSED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_PAUSED, session=session)

        now = self._clock.now()
        session.status = SessionStatus.ACTIVE
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.resumed", session, now)
        return ActionResult.success(ReasonCode.SESSION_RESUMED, session=session)

    # -- end -----------------------------------------------------------------

    async def end(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            # Ending an already-ended session is idempotent.
            return ActionResult.success(ReasonCode.SESSION_ENDED, session=session)

        now = self._clock.now()
        session.status = SessionStatus.ENDED
        session.ended_at = now
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.ended", session, now, cause="client")
        return ActionResult.success(ReasonCode.SESSION_ENDED, session=session)

    # -- usage query ---------------------------------------------------------

    async def get_usage(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        # Report the authoritative daily total for the session's current local day.
        policy = await self._policies.get_by_id(tenant_id, session.policy_id)
        daily_today = 0
        family_extra: dict[str, object] = {}
        if policy is not None:
            day = local_day_key(self._clock.now(), policy.document.rules.timezone)
            daily_today = await self._ledger.get_seconds(tenant_id, session.user_id, day)
            family_extra = await self._family_usage_extra(tenant_id, session.user_id)
        return ActionResult.success(
            ReasonCode.ALLOWED,
            session=session,
            daily_today_seconds=daily_today,
            **family_extra,
        )

    # -- family helpers ------------------------------------------------------

    async def _family_pool_blocker(
        self, tenant_id: str, user_id: str, now: datetime, policy_tz: str
    ) -> ReasonCode | None:
        """启动前校验：家庭共享池中该成员今日是否还有可支取额度。"""
        if self._families is None:
            return None
        member = await self._families.get_active_member_by_user(tenant_id, user_id)
        if member is None:
            return None
        family = await self._families.get_family(tenant_id, member.family_id)
        if family is None:
            return None
        day = local_day_key(now, family.timezone)
        state = _FamilyDayState(
            family=family,
            member=member,
            pool_used=await self._families.pool_used(tenant_id, family.id, day),
            by_member=await self._families.pool_usage_by_member(tenant_id, family.id, day),
            floors={
                m.user_id: m.floor_seconds
                for m in await self._families.list_members(tenant_id, family.id)
            },
        )
        self_used = state.by_member.get(user_id, 0)
        assert self_used <= state.pool_used
        # pool_used 已包含该成员自己的用量，不能再重复扣减。
        remaining = family.daily_pool_seconds - state.pool_used - state.others_reserved()
        return None if remaining > 0 else ReasonCode.DENIED_FAMILY_POOL_REACHED

    async def _family_usage_extra(self, tenant_id: str, user_id: str) -> dict[str, object]:
        if self._families is None:
            return {}
        member = await self._families.get_active_member_by_user(tenant_id, user_id)
        if member is None:
            return {}
        family = await self._families.get_family(tenant_id, member.family_id)
        if family is None:
            return {}
        day = local_day_key(self._clock.now(), family.timezone)
        pool_used = await self._families.pool_used(tenant_id, family.id, day)
        member_used = await self._families.member_used(
            tenant_id, family.id, user_id, day
        )
        return {
            "family_id": family.id,
            "family_day": day,
            "family_pool_seconds": family.daily_pool_seconds,
            "family_pool_used_seconds": pool_used,
            "family_member_used_seconds": member_used,
            "family_member_floor_seconds": member.floor_seconds,
        }

    # -- helpers -------------------------------------------------------------

    async def _emit(self, event_type: str, session: Session, now, **payload) -> None:
        await self._outbox.add(
            DomainEvent(
                event_type=event_type,
                tenant_id=session.tenant_id,
                aggregate_id=session.id,
                occurred_at=now,
                payload={
                    "session_id": session.id,
                    "user_id": session.user_id,
                    "status": session.status.value,
                    **payload,
                },
            )
        )
