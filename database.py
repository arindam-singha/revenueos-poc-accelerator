import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


def initialize_database(database_path):
    with _connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                industry TEXT NOT NULL,
                brief_text TEXT NOT NULL,
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
            """
        )


def save_brief(database_path, company, industry, brief_text):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO briefs (company, industry, brief_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (company, industry, brief_text, created_at),
        )
        return cursor.lastrowid


def save_plan(database_path, brief_id, plan, sla_hours=4):
    created_at = datetime.now(timezone.utc)
    sla_due_at = created_at + timedelta(hours=sla_hours)
    with _connect(database_path) as connection:
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
            SELECT plans.id, briefs.company, plans.title, plans.status,
                   plans.created_at, plans.sla_due_at, plans.plan_json
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
                   briefs.brief_text, plans.title, plans.plan_json, plans.status,
                   plans.created_at, plans.sla_due_at
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
