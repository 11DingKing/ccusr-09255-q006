"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def make_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """执行确定性的业务处理。"""
    connect_args: dict = {}
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # 并发写事务必须排队等待而非立刻得到 locked 错误。
        connect_args["timeout"] = 30
        if ":memory:" in database_url:
            # Share one in-memory connection across all sessions so schema and
            # data persist for the lifetime of the engine (used by tests).
            kwargs["poolclass"] = StaticPool
    engine = create_async_engine(
        database_url, echo=echo, future=True, connect_args=connect_args, **kwargs
    )
    if database_url.startswith("sqlite"):
        _install_sqlite_write_locking(engine)
    return engine


def _install_sqlite_write_locking(engine: AsyncEngine) -> None:
    """所有事务以 BEGIN IMMEDIATE 开始，把写事务在数据库层串行化。

    默认的 DEFERRED 事务在“先读后写”的并发场景下可能产生锁升级死锁；
    家庭共享池扣减依赖“读已用量 -> 条件写入”的原子性，因此统一在事务
    开启时立即获取 RESERVED 锁，配合 busy_timeout 让后来者排队。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_busy_timeout(dbapi_connection, _record) -> None:  # pragma: no cover - pragmas
        # 关闭 sqlite3 驱动遗留的“DML 前隐式 BEGIN”行为，否则它会与我们显式
        # 发出的 BEGIN IMMEDIATE 冲突（cannot start a transaction within a
        # transaction）。事务边界完全交由 SQLAlchemy 的 begin/commit 控制。
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(dbapi_connection) -> None:
        dbapi_connection.exec_driver_sql("BEGIN IMMEDIATE")


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """执行确定性的业务处理。"""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


async def iter_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """执行确定性的业务处理。"""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
