"""Restart-safe-in-process background SLA polling and escalation delivery."""
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from database import mark_opportunity_trigger_notified, sync_opportunity_sla

_LOCK = threading.Lock()
_STARTED = set()
_STOP_EVENTS = {}


def _notify(trigger):
    message = (
        f"RevenueOS SLA BREACH: {trigger['opportunity_id']} ({trigger['payload'].get('account')})\n"
        f"Owner: {trigger['owner']}\nDeadline: {trigger['breach_timestamp']}\n"
        f"Brief: {trigger['payload'].get('customer_brief', '')}\n"
        f"Draft: {json.dumps(trigger['payload'].get('draft'), ensure_ascii=False)}"
    )
    print(message, flush=True)
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        body = json.dumps({"text": message}).encode("utf-8")
        request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def monitor_once(database_path):
    triggers = sync_opportunity_sla(database_path)
    for trigger in triggers:
        try:
            _notify(trigger)
            mark_opportunity_trigger_notified(database_path, trigger["event_id"])
        except Exception as error:  # notification failures must not erase the durable trigger
            mark_opportunity_trigger_notified(database_path, trigger["event_id"], error=error)
    return triggers


def _loop(database_path, interval, stop_event):
    while not stop_event.is_set():
        try:
            monitor_once(database_path)
        except Exception as error:
            print(f"RevenueOS SLA monitor error: {error}", flush=True)
        stop_event.wait(interval)


def start_sla_monitor(database_path, interval=None):
    path = str(Path(database_path).resolve())
    with _LOCK:
        if path in _STARTED:
            return False
        _STARTED.add(path)
        stop_event = threading.Event()
        _STOP_EVENTS[path] = stop_event
        seconds = max(15, int(interval or os.getenv("SLA_POLL_SECONDS", "60")))
        thread = threading.Thread(target=_loop, args=(path, seconds, stop_event), daemon=True, name="revenueos-sla-monitor")
        thread.start()
    return True


def stop_sla_monitor(database_path):
    path = str(Path(database_path).resolve())
    with _LOCK:
        stop_event = _STOP_EVENTS.pop(path, None)
        _STARTED.discard(path)
    if stop_event:
        stop_event.set()
        return True
    return False
