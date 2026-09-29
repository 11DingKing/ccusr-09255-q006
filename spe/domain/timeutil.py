"""服务端业务模块。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_now(instant: datetime, timezone: str) -> datetime:
    """执行确定性的业务处理。"""
    return instant.astimezone(ZoneInfo(timezone))


def local_time(instant: datetime, timezone: str) -> time:
    """执行确定性的业务处理。"""
    return local_now(instant, timezone).timetz().replace(tzinfo=None)


def local_day(instant: datetime, timezone: str) -> date:
    """执行确定性的业务处理。"""
    return local_now(instant, timezone).date()


def local_day_key(instant: datetime, timezone: str) -> str:
    """执行确定性的业务处理。"""
    return local_day(instant, timezone).isoformat()


def age_at(birth_date: date, instant: datetime, timezone: str) -> int:
    """执行确定性的业务处理。"""
    today = local_day(instant, timezone)
    had_birthday = (today.month, today.day) >= (birth_date.month, birth_date.day)
    return today.year - birth_date.year - (0 if had_birthday else 1)


def next_local_midnight(instant: datetime, timezone: str) -> datetime:
    """执行确定性的业务处理。"""
    tz = ZoneInfo(timezone)
    aware = instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)
    local = aware.astimezone(tz)
    next_day = local.date() + timedelta(days=1)
    midnight_local = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    return midnight_local.astimezone(UTC)


def split_watch_window(
    now: datetime, seconds: int, timezone: str
) -> list[tuple[str, int]]:
    """执行确定性的业务处理。"""
    if seconds <= 0:
        return []
    # Normalise to UTC-aware, whole-second boundaries so durations sum exactly.
    end = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    end = end.astimezone(UTC).replace(microsecond=0)
    cursor = end - timedelta(seconds=seconds)
    remaining = seconds
    segments: list[tuple[str, int]] = []
    while remaining > 0:
        day = local_day(cursor, timezone).isoformat()
        boundary = next_local_midnight(cursor, timezone)
        span = int((boundary - cursor).total_seconds())
        take = remaining if span <= 0 or span >= remaining else span
        segments.append((day, take))
        remaining -= take
        cursor = cursor + timedelta(seconds=take)
    return segments


def split_watch_window_multi(
    now: datetime, seconds: int, timezones: list[str]
) -> list[tuple[int, list[str]]]:
    """按多个时区的本地午夜切分同一观看窗口。

    返回 ``(片段秒数, 每个时区对应的 local_day)`` 的有序列表，日键顺序与
    入参 ``timezones`` 一致；所有片段秒数之和恰为 ``seconds``。
    """
    if seconds <= 0:
        return []
    end = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    end = end.astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(seconds=seconds)

    boundaries: set[datetime] = {end}
    for tz in timezones:
        cursor = start
        while True:
            boundary = next_local_midnight(cursor, tz)
            if boundary > end:
                break
            boundaries.add(boundary)
            cursor = boundary

    result: list[tuple[int, list[str]]] = []
    cursor = start
    for boundary in sorted(boundaries):
        take = int((boundary - cursor).total_seconds())
        if take <= 0:
            continue
        days = [local_day(cursor, tz).isoformat() for tz in timezones]
        result.append((take, days))
        cursor = boundary
    return result
