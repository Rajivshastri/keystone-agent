"""Timezone helpers — everything user-facing is Asia/Kolkata.

The Azure App Service runs Linux containers that default to UTC unless
TZ / WEBSITE_TIME_ZONE is set, so naive `datetime.now()` is unreliable
across deployments. These helpers give every caller an explicit IST
clock regardless of what the surrounding environment thinks "now" is.

Storage timestamps that other systems consume by ISO string (e.g. the
M365 Graph receivedDateTime filter) must remain UTC — use `utc_now()`
for those. Anything an operator reads should use the IST helpers.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def ist_now() -> datetime:
    """Current wall-clock time in IST as a tz-aware datetime."""
    return datetime.now(IST)


def ist_today_str() -> str:
    """IST 'YYYY-MM-DD' — 'today' from the operator's perspective."""
    return ist_now().strftime('%Y-%m-%d')


def ist_now_str(fmt: str = '%H:%M:%S') -> str:
    """IST wall-clock time formatted with `fmt` (default HH:MM:SS)."""
    return ist_now().strftime(fmt)


def utc_now() -> datetime:
    """tz-aware UTC for storage timestamps that external systems consume."""
    return datetime.now(timezone.utc)
