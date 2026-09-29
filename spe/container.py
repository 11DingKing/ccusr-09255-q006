"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from spe.config import Settings, get_settings
from spe.domain.clock import Clock, SystemClock
from spe.domain.ids import IdGenerator, UuidGenerator
from spe.domain.services.family_service import FamilyService
from spe.domain.services.policy_service import PolicyService
from spe.domain.services.session_service import SessionService
from spe.infra.db.repositories.family_repositories import SqlFamilyRepository
from spe.infra.db.repositories.repositories import (
    SqlDailyUsageLedger,
    SqlHeartbeatRepository,
    SqlOutboxRepository,
    SqlPolicyRepository,
    SqlSessionRepository,
)
from spe.infra.db.session import make_engine, make_session_factory


@dataclass
class Services:
    """封装领域状态与业务约束。"""

    policy_service: PolicyService
    session_service: SessionService
    family_service: FamilyService
    heartbeats: SqlHeartbeatRepository
    policies: SqlPolicyRepository
    ledger: SqlDailyUsageLedger
    families: SqlFamilyRepository
    clock: Clock


class Container:
    """封装领域状态与业务约束。"""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock: Clock = clock or SystemClock()
        self.ids: IdGenerator = ids or UuidGenerator()
        self.engine: AsyncEngine = make_engine(
            self.settings.database_url, echo=self.settings.echo_sql
        )
        self.session_factory: async_sessionmaker[AsyncSession] = make_session_factory(
            self.engine
        )

    def services_for(self, db: AsyncSession) -> Services:
        """执行确定性的业务处理。"""
        now = self.clock.now()
        policies = SqlPolicyRepository(db, clock_now=now)
        sessions = SqlSessionRepository(db)
        outbox = SqlOutboxRepository(db)
        heartbeats = SqlHeartbeatRepository(db)
        ledger = SqlDailyUsageLedger(db)
        families = SqlFamilyRepository(db, clock_now=now)
        policy_service = PolicyService(policies, outbox, self.clock, self.ids)
        session_service = SessionService(
            sessions,
            policies,
            outbox,
            self.clock,
            self.ids,
            ledger=ledger,
            heartbeats=heartbeats,
            heartbeat_max_gap_seconds=self.settings.heartbeat_max_gap_seconds,
            families=families,
        )
        family_service = FamilyService(families, outbox, self.clock, self.ids)
        return Services(
            policy_service=policy_service,
            session_service=session_service,
            family_service=family_service,
            heartbeats=heartbeats,
            policies=policies,
            ledger=ledger,
            families=families,
            clock=self.clock,
        )

    async def dispose(self) -> None:
        await self.engine.dispose()
