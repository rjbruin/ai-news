from app.services.schedule_text import describe_schedule


def test_daily_consecutive_run():
    assert describe_schedule("day", {"release_days": [0, 1, 2, 3], "release_time": "08:00"}) == \
        "every Monday-Thursday at 08:00 UTC"


def test_daily_non_consecutive():
    assert describe_schedule("day", {"release_days": [0, 2], "release_time": "07:00"}) == \
        "every Monday and Wednesday at 07:00 UTC"


def test_daily_every_day():
    assert describe_schedule("day", {"release_days": [0, 1, 2, 3, 4, 5, 6], "release_time": "08:00"}) == \
        "every day at 08:00 UTC"


def test_daily_single_day():
    assert describe_schedule("day", {"release_days": [4], "release_time": "08:00"}) == \
        "every Friday at 08:00 UTC"


def test_daily_three_pieces():
    assert describe_schedule("day", {"release_days": [0, 2, 4], "release_time": "08:00"}) == \
        "every Monday, Wednesday and Friday at 08:00 UTC"


def test_daily_default_release_days_when_missing():
    assert describe_schedule("day", {}) == "every Monday-Friday at 08:00 UTC"


def test_weekly_single_day_ignores_release_time():
    assert describe_schedule("week", {"release_day": 2, "release_time": "18:00"}) == \
        "every Wednesday at 08:00 UTC"


def test_weekly_default_is_monday():
    assert describe_schedule("week", {}) == "every Monday at 08:00 UTC"
