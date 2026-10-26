from datetime import datetime, timezone


def get_sla_status(created_at, sla_due_at, status="open", now=None):
    if status.lower() in {"complete", "completed", "closed"}:
        return "Complete"
    current_time = now or datetime.now(timezone.utc)
    due_time = datetime.fromisoformat(sla_due_at)
    if due_time.tzinfo is None:
        due_time = due_time.replace(tzinfo=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    if current_time > due_time:
        return "Breached"
    minutes_remaining = int((due_time - current_time).total_seconds() // 60)
    if minutes_remaining <= 60:
        return f"At risk · {minutes_remaining}m left"
    return "On track"
