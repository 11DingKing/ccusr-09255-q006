"""多时区观看窗口切分。"""

from __future__ import annotations

from datetime import UTC, datetime

from spe.domain.timeutil import split_watch_window_multi


def test_single_timezone_matches_sum() -> None:
    now = datetime(2026, 7, 24, 4, 0, 30, tzinfo=UTC)
    segs = split_watch_window_multi(now, 60, ["UTC"])
    assert sum(s for s, _ in segs) == 60
    assert [days[0] for _, days in segs] == ["2026-07-24"]


def test_two_timezones_split_at_both_midnights() -> None:
    # 04:00:30 UTC == 00:00:30 America/New_York (July, UTC-4).
    now = datetime(2026, 7, 24, 4, 0, 30, tzinfo=UTC)
    segs = split_watch_window_multi(now, 60, ["UTC", "America/New_York"])
    # 30s before NY midnight, 30s after; UTC stays on the same day.
    assert segs == [
        (30, ["2026-07-24", "2026-07-23"]),
        (30, ["2026-07-24", "2026-07-24"]),
    ]
    assert sum(s for s, _ in segs) == 60


def test_distinct_midnights_produce_three_segments() -> None:
    # Window 23:00:30..00:00:30 UTC. Tokyo (UTC+9) is already on 07-24 for the
    # whole window, while UTC flips to 07-24 at its final boundary: both day
    # keys are emitted per segment and the seconds still sum exactly.
    now = datetime(2026, 7, 24, 0, 0, 30, tzinfo=UTC)
    segs = split_watch_window_multi(now, 60 * 60, ["UTC", "Asia/Tokyo"])
    assert [days for _, days in segs] == [
        ["2026-07-23", "2026-07-24"],
        ["2026-07-24", "2026-07-24"],
    ]
    assert [secs for secs, _ in segs] == [3570, 30]
    assert sum(s for s, _ in segs) == 3600


def test_zero_and_empty() -> None:
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    assert split_watch_window_multi(now, 0, ["UTC"]) == []
