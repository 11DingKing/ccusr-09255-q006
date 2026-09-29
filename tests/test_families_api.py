"""家庭共享额度：并发扣减、跨日重置、关系变更与重启恢复。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from spe.app import create_app
from spe.config import Settings
from spe.container import Container
from spe.domain.clock import FixedClock
from spe.domain.ids import SequentialIdGenerator
from tests.conftest import TENANT_A, birth_for_age, headers, sample_policy

pytestmark = pytest.mark.asyncio


# -- helpers ----------------------------------------------------------------


async def _publish(client, tenant: str = TENANT_A, **kw) -> None:
    resp = await client.post(
        "/v1/policies", json={"document": sample_policy(**kw)}, headers=headers(tenant)
    )
    assert resp.status_code == 201


async def _start(client, user_id: str, age: int = 20, tenant: str = TENANT_A):
    return await client.post(
        "/v1/sessions",
        json={"user_id": user_id, "birth_date": birth_for_age(age).isoformat()},
        headers=headers(tenant),
    )


async def _hb(client, sid: str, seq: int, total: int, tenant: str = TENANT_A):
    return await client.post(
        f"/v1/sessions/{sid}/heartbeat",
        json={"seq": seq, "watched_seconds_total": total},
        headers=headers(tenant),
    )


async def _create_family(client, pool: int = 1000, tz: str = "UTC", name: str = "home"):
    resp = await client.post(
        "/v1/families",
        json={"name": name, "timezone": tz, "daily_pool_seconds": pool},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_member(client, family_id: str, user_id: str, floor: int = 0):
    resp = await client.post(
        f"/v1/families/{family_id}/members",
        json={"user_id": user_id, "floor_seconds": floor},
        headers=headers(),
    )
    return resp


async def _family_usage(client, family_id: str):
    resp = await client.get(f"/v1/families/{family_id}/usage", headers=headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _drain(client, sid: str, beats: int = 12, step: int = 90):
    """Send sequential heartbeats for one session (respects seq ordering)."""
    results = []
    for seq in range(1, beats + 1):
        results.append(await _hb(client, sid, seq, seq * step))
    return results


# -- creation / membership validation --------------------------------------


async def test_create_family_and_member_basics(client) -> None:
    family = await _create_family(client, pool=100)
    assert family["version"] == 1
    assert family["daily_pool_seconds"] == 100

    resp = await _add_member(client, family["id"], "u1", floor=60)
    assert resp.status_code == 201
    member = resp.json()
    assert member["user_id"] == "u1"
    assert member["status"] == "ACTIVE"

    # Second member whose floor would exceed the pool is rejected.
    bad = await _add_member(client, family["id"], "u2", floor=50)
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "REJECTED_FAMILY_FLOOR_EXCEEDS_POOL"

    # A floor that keeps the total within the pool is accepted.
    ok = await _add_member(client, family["id"], "u2", floor=40)
    assert ok.status_code == 201

    # Same user cannot hold two active memberships.
    other = await _create_family(client, pool=500, name="other")
    dup = await _add_member(client, other["id"], "u1", floor=0)
    assert dup.status_code == 409
    assert dup.json()["detail"]["reason"] == "REJECTED_FAMILY_MEMBER_ALREADY_ACTIVE"


async def test_adjust_pool_guards_floors_and_version(client) -> None:
    family = await _create_family(client, pool=100)
    await _add_member(client, family["id"], "u1", floor=80)

    # Lowering below the active floors is rejected.
    resp = await client.post(
        f"/v1/families/{family['id']}/adjust",
        json={"daily_pool_seconds": 50},
        headers=headers(),
    )
    assert resp.status_code == 422

    # Stale expected version conflicts.
    resp = await client.post(
        f"/v1/families/{family['id']}/adjust",
        json={"daily_pool_seconds": 200, "expected_version": 99},
        headers=headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "REJECTED_FAMILY_VERSION_CONFLICT"

    # Correct version bumps the pool.
    resp = await client.post(
        f"/v1/families/{family['id']}/adjust",
        json={"daily_pool_seconds": 200, "expected_version": 1},
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["daily_pool_seconds"] == 200
    assert resp.json()["version"] == 2


async def test_family_not_found_is_404(client) -> None:
    resp = await client.get("/v1/families/nope/usage", headers=headers())
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "REJECTED_FAMILY_NOT_FOUND"


# -- atomic pool settlement ------------------------------------------------


async def test_heartbeat_charges_personal_and_pool_together(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=100)
    await _add_member(client, family["id"], "u1", floor=0)

    sid = (await _start(client, "u1")).json()["session"]["id"]
    body = (await _hb(client, sid, 1, 60)).json()
    assert body["reason"] == "HEARTBEAT_APPLIED"
    assert body["extra"]["credited_seconds"] == 60
    assert body["extra"]["per_family_day"] == {family["id"]: {"2026-07-24": 60}}

    usage = await _family_usage(client, family["id"])
    assert usage["pool_used_seconds"] == 60
    assert usage["pool_remaining_seconds"] == 40
    member_usage = usage["members"][0]
    assert member_usage["user_id"] == "u1"
    assert member_usage["used_seconds"] == 60

    # Session usage endpoint also surfaces family bookkeeping.
    sess_usage = (
        await client.get(f"/v1/sessions/{sid}/usage", headers=headers())
    ).json()
    assert sess_usage["extra"]["family_pool_used_seconds"] == 60
    assert sess_usage["extra"]["family_member_used_seconds"] == 60


async def test_floor_protects_member_from_being_starved(client) -> None:
    # m1 can consume past its floor only up to the pool minus m2's reserved floor.
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=100)
    await _add_member(client, family["id"], "u1", floor=60)
    await _add_member(client, family["id"], "u2", floor=40)

    s1 = (await _start(client, "u1")).json()["session"]["id"]
    results = await _drain(client, s1, beats=4)  # 90..360 seconds, seq-ordered
    # u1 is capped at 60 (pool 100 - 40 reserved for u2) despite asking for more.
    usage = await _family_usage(client, family["id"])
    u1 = next(m for m in usage["members"] if m["user_id"] == "u1")
    assert u1["used_seconds"] == 60
    assert usage["pool_used_seconds"] == 60
    # The limiting heartbeat ended the session on the family pool.
    reasons = [r.json().get("reason") for r in results]
    assert "SESSION_ENDED_BY_LIMIT" in reasons

    # u2 can now claim its reserved 40; the pool reaches exactly 100.
    s2 = (await _start(client, "u2")).json()["session"]["id"]
    await _drain(client, s2, beats=4)
    usage = await _family_usage(client, family["id"])
    assert usage["pool_used_seconds"] == 100
    by_user = {m["user_id"]: m["used_seconds"] for m in usage["members"]}
    assert by_user == {"u1": 60, "u2": 40}


async def test_concurrent_sessions_never_overdraw_pool(file_client) -> None:
    await _publish(
        file_client,
        daily_limit_seconds=80000,
        session_limit_seconds=80000,
        bedtime=None,
    )
    family = await _create_family(file_client, pool=1000)
    await _add_member(file_client, family["id"], "u1", floor=300)
    await _add_member(file_client, family["id"], "u2", floor=300)

    s1 = (await _start(file_client, "u1")).json()["session"]["id"]
    s2 = (await _start(file_client, "u2")).json()["session"]["id"]

    # Both members drain concurrently (beats stay sequential within a session
    # to respect seq ordering; the two session tasks genuinely overlap).
    await asyncio.gather(_drain(file_client, s1), _drain(file_client, s2))

    usage = await _family_usage(file_client, family["id"])
    by_user = {m["user_id"]: m["used_seconds"] for m in usage["members"]}
    # Pool is fully consumed but never overdrawn; each floor is honoured.
    assert usage["pool_used_seconds"] == 1000
    assert sum(by_user.values()) == 1000
    assert by_user["u1"] >= 300
    assert by_user["u2"] >= 300
    # Nobody can exceed pool minus the other's floor.
    assert by_user["u1"] <= 700
    assert by_user["u2"] <= 700


async def test_concurrent_floors_that_fill_the_pool_are_exact(file_client) -> None:
    # Four members whose floors already sum to the whole pool: no matter the
    # interleaving, each may consume at most its own floor and the pool total
    # never exceeds its cap.
    await _publish(
        file_client,
        daily_limit_seconds=80000,
        session_limit_seconds=80000,
        bedtime=None,
    )
    family = await _create_family(file_client, pool=1000)
    floors = {"u1": 250, "u2": 250, "u3": 250, "u4": 250}
    for uid, floor in floors.items():
        await _add_member(file_client, family["id"], uid, floor=floor)

    sids = {uid: (await _start(file_client, uid)).json()["session"]["id"] for uid in floors}

    async def drain_session(uid: str):
        # Each member asks for well beyond its floor.
        return await _drain(file_client, sids[uid], beats=10)

    await asyncio.gather(*(drain_session(uid) for uid in floors))

    usage = await _family_usage(file_client, family["id"])
    by_user = {m["user_id"]: m["used_seconds"] for m in usage["members"]}
    assert usage["pool_used_seconds"] == 1000
    assert sum(by_user.values()) == 1000
    for uid, floor in floors.items():
        assert by_user[uid] == floor, (uid, by_user)


async def test_session_limit_still_applies_inside_family(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=30, bedtime=None
    )
    family = await _create_family(client, pool=1000)
    await _add_member(client, family["id"], "u1", floor=0)
    sid = (await _start(client, "u1")).json()["session"]["id"]
    body = (await _hb(client, sid, 1, 90)).json()
    assert body["reason"] == "SESSION_ENDED_BY_LIMIT"
    assert body["extra"]["limit_reason"] == "DENIED_SESSION_LIMIT_REACHED"
    assert body["extra"]["credited_seconds"] == 30
    usage = await _family_usage(client, family["id"])
    assert usage["pool_used_seconds"] == 30


async def test_start_rejected_when_family_allowance_zero(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=60)
    await _add_member(client, family["id"], "u1", floor=60)
    sid = (await _start(client, "u1")).json()["session"]["id"]
    await _hb(client, sid, 1, 60)
    await client.post(f"/v1/sessions/{sid}/end", headers=headers())
    # Pool exhausted for u1 -> a fresh start is refused on family grounds.
    resp = await _start(client, "u1")
    assert resp.json()["ok"] is False
    assert resp.json()["reason"] == "DENIED_FAMILY_POOL_REACHED"


# -- cross-day reset --------------------------------------------------------


async def test_pool_resets_on_family_local_day(client, clock) -> None:
    family_tz = "America/New_York"  # local midnight == 04:00 UTC in July
    await _publish(
        client,
        timezone=family_tz,
        daily_limit_seconds=80000,
        session_limit_seconds=80000,
        bedtime=None,
    )
    family = await _create_family(client, pool=100, tz=family_tz)
    await _add_member(client, family["id"], "u1", floor=0)

    # 2026-07-24 03:59 UTC (still local 07-23 23:59): consume the 07-23 pool.
    # The heartbeat gap is clamped to 90s, so drain the 100s pool in two beats.
    clock.set(datetime(2026, 7, 24, 3, 59, 0, tzinfo=UTC))
    sid = (await _start(client, "u1")).json()["session"]["id"]
    await _hb(client, sid, 1, 90)
    await _hb(client, sid, 2, 100)
    usage = await _family_usage(client, family["id"])
    assert usage["local_day"] == "2026-07-23"
    assert usage["pool_used_seconds"] == 100

    # Cross local midnight into 07-24: the pool resets and usage flows again.
    clock.set(datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC))
    await client.post(f"/v1/sessions/{sid}/end", headers=headers())
    sid2 = (await _start(client, "u1")).json()["session"]["id"]
    body = (await _hb(client, sid2, 1, 60)).json()
    assert body["extra"]["per_family_day"] == {family["id"]: {"2026-07-24": 60}}
    usage = await _family_usage(client, family["id"])
    assert usage["local_day"] == "2026-07-24"
    assert usage["pool_used_seconds"] == 60
    assert usage["pool_remaining_seconds"] == 40


async def test_heartbeat_straddling_family_midnight_uses_two_day_keys(client, clock) -> None:
    # Policy keeps UTC days; the family keeps New York days (midnight 04:00 UTC).
    await _publish(
        client,
        timezone="UTC",
        daily_limit_seconds=80000,
        session_limit_seconds=80000,
        bedtime=None,
    )
    family = await _create_family(client, pool=1000, tz="America/New_York")
    await _add_member(client, family["id"], "u1", floor=0)
    sid = (await _start(client, "u1")).json()["session"]["id"]

    # 60s ending 04:00:30 UTC: personal day is all 07-24; family day splits.
    clock.set(datetime(2026, 7, 24, 4, 0, 30, tzinfo=UTC))
    body = (await _hb(client, sid, 1, 60)).json()
    assert body["extra"]["per_day"] == {"2026-07-24": 60}
    assert body["extra"]["per_family_day"] == {
        family["id"]: {"2026-07-23": 30, "2026-07-24": 30}
    }


# -- relationship changes ---------------------------------------------------


async def test_removed_member_usage_is_not_refunded(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=100)
    await _add_member(client, family["id"], "u1", floor=80)
    m2 = (await _add_member(client, family["id"], "u2", floor=20)).json()

    s2 = (await _start(client, "u2")).json()["session"]["id"]
    await _hb(client, s2, 1, 20)  # u2 uses its 20s floor
    s1 = (await _start(client, "u1")).json()["session"]["id"]
    await _hb(client, s1, 1, 80)  # pool reaches 100

    # Remove u2: its consumed 20s stays in the pool (no refund).
    resp = await client.delete(f"/v1/families/members/{m2['id']}", headers=headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "REMOVED"

    usage = await _family_usage(client, family["id"])
    assert usage["pool_used_seconds"] == 100  # still 100, not 80
    removed = next(m for m in usage["members"] if m["user_id"] == "u2")
    assert removed["status"] == "REMOVED"
    assert removed["used_seconds"] == 20
    assert removed["floor_seconds"] == 0  # floors of removed members are released

    # u1 still cannot watch: the removed member's usage was not returned.
    # (s1 already auto-ended when it hit the family-pool limit at 80s.)
    denied = await _start(client, "u1")
    assert denied.status_code == 200
    assert denied.json()["reason"] == "DENIED_FAMILY_POOL_REACHED"

    # The removed user no longer settles against the (full) family pool and can
    # keep watching under its personal policy limits.
    await client.post(f"/v1/sessions/{s2}/end", headers=headers())
    free = (await _start(client, "u2")).json()["session"]["id"]
    body = (await _hb(client, free, 1, 30)).json()
    assert body["reason"] == "HEARTBEAT_APPLIED"
    assert body["extra"]["credited_seconds"] == 30
    assert "per_family_day" not in body["extra"] or body["extra"]["per_family_day"] == {}

    # The user can join a family again after removal.
    re_add = await _add_member(client, family["id"], "u2", floor=20)
    assert re_add.status_code == 201


async def test_floor_adjustment_changes_reservation(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=100)
    m1 = (await _add_member(client, family["id"], "u1", floor=50)).json()
    m2 = (await _add_member(client, family["id"], "u2", floor=50)).json()

    s1 = (await _start(client, "u1")).json()["session"]["id"]
    body = (await _hb(client, s1, 1, 90)).json()
    # Capped at 50 while u2's 50 is reserved; the limit ends the session.
    assert body["extra"]["credited_seconds"] == 50

    # u1's own floor cannot grow to 100 while u2 still holds 50.
    resp = await client.post(
        f"/v1/families/members/{m1['id']}/floor",
        json={"floor_seconds": 100},
        headers=headers(),
    )
    assert resp.status_code == 422

    # Release u2's floor to 0; u1 may now consume the other 50 seconds.
    resp = await client.post(
        f"/v1/families/members/{m2['id']}/floor",
        json={"floor_seconds": 0},
        headers=headers(),
    )
    assert resp.status_code == 200

    await client.post(f"/v1/sessions/{s1}/end", headers=headers())
    s1b = (await _start(client, "u1")).json()["session"]["id"]
    body = (await _hb(client, s1b, 1, 90)).json()
    assert body["extra"]["credited_seconds"] == 50
    usage = await _family_usage(client, family["id"])
    assert usage["pool_used_seconds"] == 100


async def test_user_level_usage_lookup(client) -> None:
    await _publish(
        client, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
    )
    family = await _create_family(client, pool=100)
    await _add_member(client, family["id"], "u1", floor=40)
    sid = (await _start(client, "u1")).json()["session"]["id"]
    await _hb(client, sid, 1, 25)

    resp = await client.get("/v1/families/users/u1/usage", headers=headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["member"]["used_seconds"] == 25
    assert body["member"]["floor_seconds"] == 40
    assert body["usage"]["pool_used_seconds"] == 25

    # A user without membership is a 404.
    missing = await client.get("/v1/families/users/nope/usage", headers=headers())
    assert missing.status_code == 404


# -- restart recovery -------------------------------------------------------


async def _reopen_file_app(db_file, clock: FixedClock) -> tuple[Container, AsyncClient]:
    container = Container(
        settings=Settings(
            database_url=f"sqlite+aiosqlite:///{db_file}", heartbeat_max_gap_seconds=90
        ),
        clock=clock,
        ids=SequentialIdGenerator("id"),
    )
    app = create_app(container)
    app.state.container = container
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    return container, ac


async def test_family_state_recovers_after_restart(tmp_path, clock) -> None:
    db_file = tmp_path / "family_restart.db"
    container = Container(
        settings=Settings(
            database_url=f"sqlite+aiosqlite:///{db_file}", heartbeat_max_gap_seconds=90
        ),
        clock=clock,
        ids=SequentialIdGenerator("id"),
    )
    from sqlalchemy import text

    from spe.infra.db.base import Base
    from spe.infra.db.ddl import (
        CREATE_ACTIVE_FAMILY_MEMBER_INDEX,
        CREATE_ACTIVE_SESSION_INDEX,
    )

    async with container.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(CREATE_ACTIVE_SESSION_INDEX))
        await conn.execute(text(CREATE_ACTIVE_FAMILY_MEMBER_INDEX))
    app = create_app(container)
    app.state.container = container
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _publish(
            ac, daily_limit_seconds=80000, session_limit_seconds=80000, bedtime=None
        )
        family = await _create_family(ac, pool=100)
        await _add_member(ac, family["id"], "u1", floor=60)
        # A second member holding a 40s floor reserves the rest of the pool,
        # so u1's own allowance is exactly the 60s it is about to consume.
        await _add_member(ac, family["id"], "u2", floor=40)
        sid = (await _start(ac, "u1")).json()["session"]["id"]
        await _hb(ac, sid, 1, 60)
        await ac.post(f"/v1/sessions/{sid}/end", headers=headers())

    # Simulate a process restart: dispose everything and reopen the same file.
    await container.dispose()
    clock2 = FixedClock(clock.now())
    container2, ac2 = await _reopen_file_app(db_file, clock2)
    try:
        # Family config, version, membership and consumed pool all persisted.
        resp = await ac2.get(f"/v1/families/{family['id']}", headers=headers())
        assert resp.status_code == 200
        assert resp.json()["daily_pool_seconds"] == 100

        usage = await _family_usage(ac2, family["id"])
        assert usage["pool_used_seconds"] == 60
        assert usage["members"][0]["floor_seconds"] == 60
        assert usage["members"][0]["status"] == "ACTIVE"

        # Settlement keeps enforcing the restored pool: no more room for u1.
        refused = await _start(ac2, "u1")
        assert refused.json()["reason"] == "DENIED_FAMILY_POOL_REACHED"
    finally:
        await ac2.aclose()
        await container2.dispose()
