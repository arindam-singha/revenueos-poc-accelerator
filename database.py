import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sla_monitor import get_sla_deadlines, get_sla_status


@contextmanager
def _connect(database_path):
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path, briefs_path=None):
    with _connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                industry TEXT NOT NULL,
                brief_text TEXT NOT NULL,
                opportunity_id TEXT,
                owner TEXT,
                region TEXT,
                event_timestamp TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id INTEGER NOT NULL REFERENCES briefs(id),
                title TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                sla_due_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL REFERENCES plans(id),
                decision TEXT NOT NULL CHECK (decision IN ('accept', 'edit', 'reject')),
                reviewer_role TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                edit_details TEXT,
                rejection_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS opportunity_events (
                opportunity_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                owner TEXT NOT NULL,
                account TEXT NOT NULL,
                region TEXT NOT NULL,
                customer_brief TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sla_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id INTEGER NOT NULL REFERENCES briefs(id),
                plan_id INTEGER NOT NULL UNIQUE REFERENCES plans(id),
                owner TEXT NOT NULL,
                breach_timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                current_draft_reference TEXT NOT NULL,
                draft_json TEXT NOT NULL
            );
            """
        )
        brief_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(briefs)").fetchall()
        }
        for column_name in (
            "opportunity_id",
            "owner",
            "region",
            "event_timestamp",
        ):
            if column_name not in brief_columns:
                connection.execute(
                    f"ALTER TABLE briefs ADD COLUMN {column_name} TEXT"
                )
    if briefs_path is not None:
        seed_opportunity_events(database_path, briefs_path)


def seed_opportunity_events(database_path, briefs_path):
    path = Path(briefs_path)
    with path.open(encoding="utf-8") as file:
        events = json.load(file)
    if not isinstance(events, list):
        raise ValueError("Opportunity event seed data must be a JSON array.")

    required_fields = {
        "opportunity_id",
        "timestamp",
        "owner",
        "account",
        "region",
        "customer_brief",
    }
    seen_ids = set()
    for event in events:
        if not isinstance(event, dict) or not required_fields.issubset(event):
            raise ValueError(
                "Each opportunity event must include opportunity_id, timestamp, "
                "owner, account, region, and customer_brief."
            )
        opportunity_id = event["opportunity_id"]
        if not isinstance(opportunity_id, str) or not opportunity_id.strip():
            raise ValueError("Opportunity IDs must be non-empty strings.")
        if opportunity_id in seen_ids:
            raise ValueError(f"Duplicate opportunity ID in seed data: {opportunity_id}")
        seen_ids.add(opportunity_id)
        for field in ("owner", "account", "region", "customer_brief"):
            if not isinstance(event[field], str) or not event[field].strip():
                raise ValueError(f"Opportunity event field {field} must not be empty.")
        get_sla_deadlines(event["timestamp"])

    with _connect(database_path) as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO opportunity_events (
                opportunity_id, event_id, event_type, event_timestamp,
                owner, account, region, customer_brief
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event["opportunity_id"],
                    event.get("event_id", event["opportunity_id"]),
                    event.get("event_type", "opportunity_won"),
                    event["timestamp"],
                    event["owner"].strip(),
                    event["account"].strip(),
                    event["region"].strip(),
                    event["customer_brief"].strip(),
                )
                for event in events
            ],
        )


def get_opportunity_events(database_path):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_id, event_id, event_type, event_timestamp,
                   owner, account, region, customer_brief
            FROM opportunity_events
            ORDER BY event_timestamp DESC, opportunity_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_brief(
    database_path,
    company,
    industry,
    brief_text,
    event_timestamp,
    owner,
    opportunity_id=None,
    region=None,
):
    if not owner or not owner.strip():
        raise ValueError("Opportunity owner is required for SLA monitoring.")
    get_sla_deadlines(event_timestamp)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(database_path) as connection:
        if opportunity_id:
            source_event = connection.execute(
                """
                SELECT event_timestamp, owner, account, region
                FROM opportunity_events
                WHERE opportunity_id = ?
                """,
                (opportunity_id,),
            ).fetchone()
            if source_event is not None and (
                event_timestamp != source_event["event_timestamp"]
                or owner.strip() != source_event["owner"]
                or company.strip() != source_event["account"]
                or region != source_event["region"]
            ):
                raise ValueError(
                    "Opportunity account, owner, region, and event timestamp must "
                    "match the selected source event."
                )
        cursor = connection.execute(
            """
            INSERT INTO briefs (
                company, industry, brief_text, opportunity_id, owner, region,
                event_timestamp, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company,
                industry,
                brief_text,
                opportunity_id,
                owner.strip(),
                region,
                event_timestamp,
                created_at,
            ),
        )
        return cursor.lastrowid


def save_plan(database_path, brief_id, plan):
    created_at = datetime.now(timezone.utc)
    with _connect(database_path) as connection:
        brief = connection.execute(
            "SELECT event_timestamp FROM briefs WHERE id = ?",
            (brief_id,),
        ).fetchone()
        if brief is None:
            raise ValueError(f"Brief {brief_id} does not exist.")
        _, sla_due_at = get_sla_deadlines(brief["event_timestamp"])
        cursor = connection.execute(
            """
            INSERT INTO plans (brief_id, title, plan_json, status, created_at, sla_due_at)
            VALUES (?, ?, ?, 'open', ?, ?)
            """,
            (
                brief_id,
                plan["title"],
                json.dumps(plan),
                created_at.isoformat(timespec="seconds"),
                sla_due_at.isoformat(timespec="seconds"),
            ),
        )
        return cursor.lastrowid


def get_recent_plans(database_path, limit=10):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT plans.id, plans.brief_id, briefs.company, briefs.industry,
                   briefs.brief_text, briefs.opportunity_id, briefs.owner,
                   briefs.region, briefs.event_timestamp, plans.title,
                   plans.status, plans.created_at, plans.sla_due_at,
                   plans.plan_json,
                   (
                       SELECT MIN(review_decisions.timestamp)
                       FROM review_decisions
                       WHERE review_decisions.plan_id = plans.id
                   ) AS first_pickup_at
            FROM plans
            JOIN briefs ON briefs.id = plans.brief_id
            ORDER BY plans.created_at DESC, plans.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_plan(database_path, plan_id):
    with _connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT plans.id, plans.brief_id, briefs.company, briefs.industry,
                   briefs.brief_text, briefs.opportunity_id, briefs.owner,
                   briefs.region, briefs.event_timestamp, plans.title,
                   plans.plan_json, plans.status, plans.created_at,
                   plans.sla_due_at
            FROM plans
            JOIN briefs ON briefs.id = plans.brief_id
            WHERE plans.id = ?
            """,
            (plan_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["plan"] = json.loads(result["plan_json"])
    return result


def sync_sla_triggers(database_path, now=None):
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("Current time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)

    with _connect(database_path) as connection:
        plans = connection.execute(
            """
            SELECT plans.id AS plan_id, plans.brief_id, plans.plan_json,
                   briefs.owner, briefs.event_timestamp,
                   (
                       SELECT MIN(review_decisions.timestamp)
                       FROM review_decisions
                       WHERE review_decisions.plan_id = plans.id
                   ) AS first_pickup_at
            FROM plans
            JOIN briefs ON briefs.id = plans.brief_id
            WHERE briefs.event_timestamp IS NOT NULL
            """
        ).fetchall()
        for row in plans:
            status = get_sla_status(
                row["event_timestamp"],
                now=current_time,
                first_pickup_at=row["first_pickup_at"],
            )
            if status != "Red":
                continue
            _, due_at = get_sla_deadlines(row["event_timestamp"])
            connection.execute(
                """
                INSERT OR IGNORE INTO sla_triggers (
                    brief_id, plan_id, owner, breach_timestamp, status,
                    current_draft_reference, draft_json
                )
                VALUES (?, ?, ?, ?, 'Red', ?, ?)
                """,
                (
                    row["brief_id"],
                    row["plan_id"],
                    row["owner"] or "Unassigned",
                    due_at.isoformat(timespec="seconds"),
                    f'plans:{row["plan_id"]}',
                    row["plan_json"],
                ),
            )


def get_sla_triggers(database_path, limit=50):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, brief_id, plan_id, owner, breach_timestamp, status,
                   current_draft_reference, draft_json
            FROM sla_triggers
            ORDER BY breach_timestamp DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_review_decisions(database_path, plan_id):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, plan_id, decision, reviewer_role, timestamp,
                   edit_details, rejection_reason
            FROM review_decisions
            WHERE plan_id = ?
            ORDER BY timestamp DESC, id DESC
            """,
            (plan_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_review_decision(
    database_path,
    plan_id,
    decision,
    reviewer_role,
    edited_plan=None,
    edit_details=None,
    rejection_reason=None,
):
    if decision not in {"accept", "edit", "reject"}:
        raise ValueError("Decision must be accept, edit, or reject.")
    if not isinstance(plan_id, int) or plan_id <= 0:
        raise ValueError("A valid plan ID is required.")
    if not reviewer_role or not reviewer_role.strip():
        raise ValueError("Reviewer role is required.")
    if decision == "edit":
        if (
            not isinstance(edited_plan, dict)
            or not isinstance(edited_plan.get("title"), str)
            or not edited_plan["title"].strip()
        ):
            raise ValueError("An edited plan with a title is required.")
        if edit_details is not None and not isinstance(edit_details, str):
            raise ValueError("Edit details must be text.")
    if decision == "reject" and (
        not isinstance(rejection_reason, str) or not rejection_reason.strip()
    ):
        raise ValueError("A rejection reason is required.")

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(database_path) as connection:
        plan_row = connection.execute(
            "SELECT id FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if plan_row is None:
            raise ValueError(f"Plan {plan_id} does not exist.")

        status = {
            "accept": "accepted",
            "edit": "edited",
            "reject": "rejected",
        }[decision]
        if decision == "edit":
            connection.execute(
                """
                UPDATE plans
                SET title = ?, plan_json = ?, status = ?
                WHERE id = ?
                """,
                (edited_plan["title"], json.dumps(edited_plan), status, plan_id),
            )
        else:
            connection.execute(
                "UPDATE plans SET status = ? WHERE id = ?",
                (status, plan_id),
            )
        cursor = connection.execute(
            """
            INSERT INTO review_decisions (
                plan_id, decision, reviewer_role, timestamp,
                edit_details, rejection_reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                decision,
                reviewer_role.strip(),
                timestamp,
                edit_details.strip() if edit_details else None,
                rejection_reason.strip() if rejection_reason else None,
            ),
        )
        return cursor.lastrowid
