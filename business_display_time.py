"""Format timestamps written by the application database for display in Japan."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


JST = ZoneInfo('Asia/Tokyo')


def database_event_time(value, *, postgres):
    # get_db() sets the PostgreSQL session to JST; SQLite CURRENT_TIMESTAMP is UTC.
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(instant, datetime):
            return '未確認'
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=JST if postgres else timezone.utc)
        return instant.astimezone(JST).strftime('%Y/%m/%d %H:%M')
    except (ValueError, OverflowError):
        return '未確認'
