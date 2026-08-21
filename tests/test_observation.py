from datetime import datetime, timedelta, timezone


def test_observation_datetime_converts_offsets_to_naive_utc():
    from src.observation import observation_datetime

    expected = datetime(2026, 8, 1, 0, 0)
    assert observation_datetime("2026-08-01T08:00:00+08:00") == expected
    assert observation_datetime(
        datetime(2026, 8, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    ) == expected
