"""Business date: system-local today by default, fixed by env for repeatable runs."""

from datetime import date
import os


def get_business_date() -> date:
    configured = os.getenv("BUSINESS_DATE")
    if not configured:
        return date.today()
    try:
        return date.fromisoformat(configured)
    except ValueError as exc:
        raise ValueError("BUSINESS_DATE must use YYYY-MM-DD format") from exc
