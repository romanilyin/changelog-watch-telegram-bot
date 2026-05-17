#!/usr/bin/env python3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import ChatRouting, SummarySchedule, is_summary_due, should_suppress_summary_on_startup


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> None:
    now = datetime(2026, 5, 17, 21, 0, tzinfo=timezone.utc)
    daily_20 = SummarySchedule(mode="daily", time="20:00")

    instant_none = ChatRouting(
        chat_id="185073278",
        groups=set(),
        source_ids=set(),
        delivery_mode="instant",
        summary_schedule=SummarySchedule.disabled(),
    )
    should_send_summary = instant_none.delivery_mode in {"digest", "both"} and instant_none.summary_schedule.mode != "none"
    assert_true(not should_send_summary, "instant + summary none must not send digest")
    assert_true(not is_summary_due(instant_none.summary_schedule, now, None), "disabled summary schedule must not be due")

    digest_default = ChatRouting(
        chat_id="185073278",
        groups=set(),
        source_ids=set(),
        delivery_mode="digest",
        summary_schedule=daily_20,
        summary_on_startup=False,
    )
    assert_true(is_summary_due(daily_20, now, None), "daily digest should be due after schedule time")
    suppressed, boundary = should_suppress_summary_on_startup(digest_default, now, now)
    assert_true(suppressed, "summary_on_startup=false should suppress due digest at startup")
    assert_true(boundary == datetime(2026, 5, 17, 20, 0, tzinfo=timezone.utc), "suppressed boundary should be daily schedule")

    next_day = datetime(2026, 5, 18, 20, 1, tzinfo=timezone.utc)
    assert_true(is_summary_due(daily_20, next_day, boundary.isoformat()), "next day digest should be due after marked boundary")

    digest_on_startup = ChatRouting(
        chat_id="185073278",
        groups=set(),
        source_ids=set(),
        delivery_mode="digest",
        summary_schedule=daily_20,
        summary_on_startup=True,
    )
    suppressed, _ = should_suppress_summary_on_startup(digest_on_startup, now, now)
    assert_true(not suppressed, "summary_on_startup=true should allow due digest at startup")

    print("summary startup checks passed")


if __name__ == "__main__":
    main()
