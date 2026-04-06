from __future__ import annotations

import calendar
from datetime import date

from app.config import Settings
from app.schemas import ChildAgeSnapshot


class ChildAgeService:
    def __init__(self, settings: Settings) -> None:
        self._birth_date = date.fromisoformat(settings.child_birth_date)

    def calculate(self, as_of_date: date | None = None) -> ChildAgeSnapshot:
        as_of = as_of_date or date.today()
        months = (as_of.year - self._birth_date.year) * 12 + (as_of.month - self._birth_date.month)
        anchor = self._add_months(self._birth_date, months)
        if as_of < anchor:
            months -= 1
            anchor = self._add_months(self._birth_date, months)
        days = (as_of - anchor).days
        d_plus = (as_of - self._birth_date).days + 1
        return ChildAgeSnapshot(
            birth_date=self._birth_date,
            as_of_date=as_of,
            d_plus=d_plus,
            months=months,
            days=days,
        )

    @staticmethod
    def _add_months(source: date, months: int) -> date:
        month_index = source.month - 1 + months
        year = source.year + month_index // 12
        month = month_index % 12 + 1
        day = min(source.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
