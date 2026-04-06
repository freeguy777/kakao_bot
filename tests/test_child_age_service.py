from __future__ import annotations

from datetime import date

from app.services.child_age_service import ChildAgeService


def test_child_age_calendar_month_difference(test_settings) -> None:
    service = ChildAgeService(test_settings)
    snapshot = service.calculate(date(2026, 4, 6))
    assert snapshot.months == 18
    assert snapshot.days == 11
    assert snapshot.d_plus == 558
