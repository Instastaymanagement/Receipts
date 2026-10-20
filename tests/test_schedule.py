from datetime import datetime, timezone

from receipt_bot.schedule import should_run


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_summer_runs_at_01_utc_only():
    # Oct 21 01:05 UTC = Oct 20 9:05 pm EDT
    assert should_run("schedule", "0 1 * * *", utc(2026, 10, 21, 1, 5))
    assert not should_run("schedule", "0 2 * * *", utc(2026, 10, 21, 2, 5))


def test_winter_runs_at_02_utc_only():
    # Dec 2 02:10 UTC = Dec 1 9:10 pm EST
    assert should_run("schedule", "0 2 * * *", utc(2026, 12, 2, 2, 10))
    assert not should_run("schedule", "0 1 * * *", utc(2026, 12, 2, 1, 10))


def test_dst_end_day():
    # DST ends Nov 1 2026; 9 pm that evening is EST = 02:00 UTC Nov 2
    assert should_run("schedule", "0 2 * * *", utc(2026, 11, 2, 2, 20))
    assert not should_run("schedule", "0 1 * * *", utc(2026, 11, 2, 1, 20))


def test_late_start_still_counts():
    assert should_run("schedule", "0 1 * * *", utc(2026, 7, 1, 1, 45))


def test_manual_always_runs():
    assert should_run("workflow_dispatch", "", utc(2026, 7, 1, 15, 0))
