from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
BUSINESS_DAYS_FOR_SLA = 2
AMBER_BUSINESS_DAYS = 1


def parse_event_timestamp(timestamp):
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("An original opportunity event timestamp is required.")
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("Timestamp must be valid ISO 8601.") from error
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset.")
    return parsed.astimezone(BUSINESS_TIMEZONE)


def _add_business_days(timestamp, days):
    result = timestamp.astimezone(BUSINESS_TIMEZONE)
    remaining = days
    while remaining:
        result += timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def get_sla_deadlines(event_timestamp):
    event_time = parse_event_timestamp(event_timestamp)
    amber_at = _add_business_days(event_time, AMBER_BUSINESS_DAYS)
    due_at = _add_business_days(event_time, BUSINESS_DAYS_FOR_SLA)
    return amber_at, due_at


def get_sla_status(event_timestamp, now=None, first_pickup_at=None):
    amber_at, due_at = get_sla_deadlines(event_timestamp)
    current_time = now or datetime.now(BUSINESS_TIMEZONE)
    if current_time.tzinfo is None:
        raise ValueError("Current time must include a timezone.")
    current_time = current_time.astimezone(BUSINESS_TIMEZONE)

    if first_pickup_at:
        pickup_time = parse_event_timestamp(first_pickup_at)
        if pickup_time <= due_at:
            return "Green"
        return "Red"
    if current_time >= due_at:
        return "Red"
    if current_time >= amber_at:
        return "Amber"
    return "Green"


def get_business_day_age(event_timestamp, now=None):
    event_time = parse_event_timestamp(event_timestamp)
    current_time = now or datetime.now(BUSINESS_TIMEZONE)
    if current_time.tzinfo is None:
        raise ValueError("Current time must include a timezone.")
    current_time = current_time.astimezone(BUSINESS_TIMEZONE)
    if current_time <= event_time:
        return 0

    business_days = 0
    cursor = event_time.date()
    while cursor < current_time.date():
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            business_days += 1
    return business_days
