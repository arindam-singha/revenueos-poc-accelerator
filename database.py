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
                opportunity_id TEXT,
                decision TEXT NOT NULL CHECK (decision IN ('accept', 'edit', 'reject')),
                reviewer_role TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                edit_details TEXT,
                rejection_reason TEXT,
                edited_plan_json TEXT,
                template_ids_json TEXT NOT NULL DEFAULT '[]',
                retained_template_ids_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS plan_templates (
                plan_id INTEGER NOT NULL REFERENCES plans(id),
                template_id TEXT NOT NULL,
                template_title TEXT NOT NULL,
                PRIMARY KEY (plan_id, template_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                opportunity_id TEXT,
                brief_id INTEGER REFERENCES briefs(id),
                plan_id INTEGER REFERENCES plans(id),
                timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opportunity_events (
                opportunity_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                owner TEXT NOT NULL,
                account TEXT NOT NULL,
                region TEXT NOT NULL,
                customer_brief TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'seed',
                segment TEXT,
                regulator TEXT,
                systems_json TEXT NOT NULL DEFAULT '[]',
                expected_timeline TEXT,
                success_criteria_json TEXT NOT NULL DEFAULT '[]',
                received_at TEXT
            );
            CREATE TABLE IF NOT EXISTS opportunity_sla_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                opportunity_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                breach_timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                notified_at TEXT,
                notification_error TEXT
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
        opportunity_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(opportunity_events)").fetchall()
        }
        for column_name, column_type in (
            ("source", "TEXT NOT NULL DEFAULT 'seed'"),
            ("segment", "TEXT"),
            ("regulator", "TEXT"),
            ("systems_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("expected_timeline", "TEXT"),
            ("success_criteria_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("received_at", "TEXT"),
        ):
            if column_name not in opportunity_columns:
                connection.execute(
                    f"ALTER TABLE opportunity_events ADD COLUMN {column_name} {column_type}"
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
        decision_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(review_decisions)"
            ).fetchall()
        }
        for column_name, column_type in (
            ("opportunity_id", "TEXT"),
            ("edited_plan_json", "TEXT"),
            ("template_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("retained_template_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column_name not in decision_columns:
                connection.execute(
                    f"ALTER TABLE review_decisions ADD COLUMN "
                    f"{column_name} {column_type}"
                )
        plans = connection.execute(
            """
            SELECT plans.id, plans.plan_json, briefs.opportunity_id
            FROM plans
            JOIN briefs ON briefs.id = plans.brief_id
            """
        ).fetchall()
        for plan_row in plans:
            plan = json.loads(plan_row["plan_json"])
            template_titles = {
                item["id"]: item.get("title", item["id"])
                for item in plan.get("evidence", [])
                if isinstance(item, dict)
                and item.get("type") == "template"
                and item.get("id")
            }
            connection.executemany(
                """
                INSERT OR IGNORE INTO plan_templates (
                    plan_id, template_id, template_title
                )
                VALUES (?, ?, ?)
                """,
                [
                    (plan_row["id"], template_id, title)
                    for template_id, title in template_titles.items()
                ],
            )
        legacy_decisions = connection.execute(
            """
            SELECT review_decisions.id, review_decisions.plan_id,
                   review_decisions.decision, plans.plan_json,
                   briefs.opportunity_id
            FROM review_decisions
            JOIN plans ON plans.id = review_decisions.plan_id
            JOIN briefs ON briefs.id = plans.brief_id
            WHERE review_decisions.template_ids_json = '[]'
            """
        ).fetchall()
        for decision in legacy_decisions:
            plan = json.loads(decision["plan_json"])
            template_ids = _plan_template_ids(plan)
            retained_ids = (
                template_ids if decision["decision"] in {"accept", "edit"} else []
            )
            connection.execute(
                """
                UPDATE review_decisions
                SET opportunity_id = ?, template_ids_json = ?,
                    retained_template_ids_json = ?
                WHERE id = ?
                """,
                (
                    decision["opportunity_id"],
                    json.dumps(template_ids),
                    json.dumps(retained_ids),
                    decision["id"],
                ),
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



def ingest_opportunity_event(database_path, event):
    """Persist one CRM opportunity-won event. event_id is the idempotency key."""
    required = ("event_id", "opportunity_id", "timestamp", "owner", "account", "region", "customer_brief")
    missing = [key for key in required if not isinstance(event.get(key), str) or not event[key].strip()]
    if missing:
        raise ValueError("Missing or empty required fields: " + ", ".join(missing))
    if event.get("event_type", "opportunity_won") != "opportunity_won":
        raise ValueError("Only opportunity_won events are supported by this endpoint.")
    get_sla_deadlines(event["timestamp"])
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(database_path) as connection:
        existing = connection.execute(
            "SELECT event_id, opportunity_id FROM opportunity_events WHERE event_id = ?",
            (event["event_id"].strip(),),
        ).fetchone()
        if existing:
            if existing["opportunity_id"] != event["opportunity_id"].strip():
                raise ValueError("This event_id is already associated with a different opportunity_id.")
            return {"created": False, "duplicate": True, "opportunity_id": existing["opportunity_id"], "event_id": existing["event_id"]}
        try:
            connection.execute(
                """INSERT INTO opportunity_events (
                    opportunity_id, event_id, event_type, event_timestamp, owner,
                    account, region, customer_brief, source, segment, regulator,
                    systems_json, expected_timeline, success_criteria_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["opportunity_id"].strip(), event["event_id"].strip(),
                    "opportunity_won", event["timestamp"], event["owner"].strip(),
                    event["account"].strip(), event["region"].strip(),
                    event["customer_brief"].strip(), event.get("source", "crm_webhook"),
                    event.get("segment"), event.get("regulator"),
                    json.dumps(event.get("systems", [])), event.get("expected_timeline"),
                    json.dumps(event.get("success_criteria", [])), received_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("An opportunity with this opportunity_id already exists under a different event_id.") from error
        connection.execute(
            "INSERT INTO workflow_events (event_type, opportunity_id, timestamp, payload_json) VALUES (?, ?, ?, ?)",
            ("opportunity_won_ingested", event["opportunity_id"].strip(), received_at, json.dumps({"event_id": event["event_id"], "source": event.get("source", "crm_webhook")})),
        )
    return {"created": True, "duplicate": False, "opportunity_id": event["opportunity_id"], "event_id": event["event_id"]}


def get_opportunity_sla_triggers(database_path, limit=50):
    with _connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM opportunity_sla_triggers ORDER BY breach_timestamp DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["payload"] = json.loads(row.pop("payload_json"))
    return result


def sync_opportunity_sla(database_path, now=None):
    """Create one durable escalation per unpicked opportunity after its SLA."""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("Current time must include a timezone.")
    with _connect(database_path) as connection:
        rows = connection.execute(
            """SELECT e.*,
               (SELECT MIN(p.created_at) FROM briefs b JOIN plans p ON p.brief_id=b.id
                WHERE b.opportunity_id=e.opportunity_id) AS first_pickup_at,
               (SELECT p.plan_json FROM briefs b JOIN plans p ON p.brief_id=b.id
                WHERE b.opportunity_id=e.opportunity_id ORDER BY p.created_at LIMIT 1) AS plan_json
               FROM opportunity_events e WHERE e.source != 'seed'"""
        ).fetchall()
        created = []
        for row in rows:
            status = get_sla_status(row["event_timestamp"], now=current_time, first_pickup_at=row["first_pickup_at"])
            if status != "Red":
                continue
            _, due_at = get_sla_deadlines(row["event_timestamp"])
            payload = {key: row[key] for key in ("event_id", "opportunity_id", "account", "owner", "region", "customer_brief", "source", "segment", "regulator", "expected_timeline")}
            payload["systems"] = json.loads(row["systems_json"] or "[]")
            payload["success_criteria"] = json.loads(row["success_criteria_json"] or "[]")
            payload["draft"] = (
                json.loads(row["plan_json"])
                if row["plan_json"]
                else {
                    "type": "intake_brief",
                    "status": "poc_plan_not_started",
                    "title": f"POC plan pending — {row['account']}",
                    "customer_brief": row["customer_brief"],
                    "systems": payload["systems"],
                    "success_criteria": payload["success_criteria"],
                }
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO opportunity_sla_triggers
                   (event_id, opportunity_id, owner, breach_timestamp, status, payload_json)
                   VALUES (?, ?, ?, ?, 'Red', ?)""",
                (row["event_id"], row["opportunity_id"], row["owner"], due_at.isoformat(timespec="seconds"), json.dumps(payload)),
            )
            if cursor.rowcount:
                created.append({"event_id": row["event_id"], "opportunity_id": row["opportunity_id"], "owner": row["owner"], "breach_timestamp": due_at.isoformat(timespec="seconds"), "payload": payload})
            elif row["plan_json"]:
                existing_trigger = connection.execute(
                    "SELECT payload_json FROM opportunity_sla_triggers WHERE event_id = ?",
                    (row["event_id"],),
                ).fetchone()
                if existing_trigger:
                    existing_payload = json.loads(existing_trigger["payload_json"])
                    existing_payload["draft"] = json.loads(row["plan_json"])
                    connection.execute(
                        "UPDATE opportunity_sla_triggers SET payload_json = ? WHERE event_id = ?",
                        (json.dumps(existing_payload), row["event_id"]),
                    )
    return created


def mark_opportunity_trigger_notified(database_path, event_id, error=None):
    with _connect(database_path) as connection:
        connection.execute(
            "UPDATE opportunity_sla_triggers SET notified_at=?, notification_error=? WHERE event_id=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds") if error is None else None, str(error)[:1000] if error else None, event_id),
        )


def get_opportunity_events(database_path):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_id, event_id, event_type, event_timestamp,
                   owner, account, region, customer_brief, source, segment,
                   regulator, systems_json, expected_timeline,
                   success_criteria_json, received_at
            FROM opportunity_events
            ORDER BY event_timestamp DESC, opportunity_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_template_reuse_metrics(database_path):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT plan_templates.plan_id, plan_templates.template_id,
                   plan_templates.template_title
            FROM plan_templates
            ORDER BY plan_templates.plan_id, plan_templates.template_id
            """
        ).fetchall()

    metrics = {}
    for row in rows:
        metric = metrics.setdefault(
            row["template_id"],
            {
                "template_id": row["template_id"],
                "title": row["template_title"],
                "draft_use_count": 0,
                "reuse_count": 0,
                "retained_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
            },
        )
        metric["draft_use_count"] += 1
        metric["reuse_count"] += 1

    for decision in _latest_review_decisions(database_path):
        template_ids = json.loads(decision["template_ids_json"] or "[]")
        retained_ids = json.loads(decision["retained_template_ids_json"] or "[]")
        for template_id in set(template_ids) | set(retained_ids):
            metrics.setdefault(
                template_id,
                {
                    "template_id": template_id,
                    "title": template_id,
                    "draft_use_count": 0,
                    "reuse_count": 0,
                    "retained_count": 0,
                    "accepted_count": 0,
                    "rejected_count": 0,
                },
            )
        for template_id in retained_ids:
            metrics[template_id]["retained_count"] += 1
        if decision["decision"] == "accept":
            for template_id in retained_ids:
                metrics[template_id]["accepted_count"] += 1
        elif decision["decision"] == "reject":
            for template_id in template_ids:
                metrics[template_id]["rejected_count"] += 1
    return sorted(
        metrics.values(),
        key=lambda item: (-item["draft_use_count"], item["title"]),
    )


def _plan_template_ids(plan):
    evidence = plan.get("evidence", [])
    if not isinstance(evidence, list):
        return []
    return list(
        dict.fromkeys(
            item["id"]
            for item in evidence
            if isinstance(item, dict)
            and item.get("type") == "template"
            and item.get("id")
        )
    )


def _latest_review_decisions(database_path):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT review_decisions.plan_id, review_decisions.decision,
                   review_decisions.template_ids_json,
                   review_decisions.retained_template_ids_json
            FROM review_decisions
            WHERE review_decisions.id IN (
                SELECT MAX(id) FROM review_decisions GROUP BY plan_id
            )
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_template_ranking_feedback(database_path):
    """Return transparent +/-0.05-per-plan feedback, bounded to +/-0.20."""
    feedback = {}
    for decision in _latest_review_decisions(database_path):
        template_ids = json.loads(decision["template_ids_json"] or "[]")
        retained_ids = set(
            json.loads(decision["retained_template_ids_json"] or "[]")
        )
        if decision["decision"] == "accept":
            for template_id in retained_ids:
                feedback[template_id] = feedback.get(template_id, 0.0) + 0.05
        elif decision["decision"] == "reject":
            for template_id in template_ids:
                feedback[template_id] = feedback.get(template_id, 0.0) - 0.05
    return {
        template_id: max(-0.2, min(0.2, adjustment))
        for template_id, adjustment in feedback.items()
    }


def add_workflow_event(
    database_path,
    event_type,
    opportunity_id=None,
    brief_id=None,
    plan_id=None,
    payload=None,
    connection=None,
):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = (
        event_type,
        opportunity_id,
        brief_id,
        plan_id,
        timestamp,
        json.dumps(payload or {}),
    )
    if connection is not None:
        cursor = connection.execute(
            """
            INSERT INTO workflow_events (
                event_type, opportunity_id, brief_id, plan_id, timestamp, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return cursor.lastrowid
    with _connect(database_path) as db_connection:
        cursor = db_connection.execute(
            """
            INSERT INTO workflow_events (
                event_type, opportunity_id, brief_id, plan_id, timestamp, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return cursor.lastrowid


def get_workflow_events(database_path, plan_id=None, opportunity_id=None, limit=100):
    clauses = []
    parameters = []
    if plan_id is not None:
        clauses.append("plan_id = ?")
        parameters.append(plan_id)
    if opportunity_id is not None:
        clauses.append("opportunity_id = ?")
        parameters.append(opportunity_id)
    where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.append(limit)
    with _connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, event_type, opportunity_id, brief_id, plan_id,
                   timestamp, payload_json
            FROM workflow_events
            {where_clause}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    result = [dict(row) for row in rows]
    for event in result:
        event["payload"] = json.loads(event.pop("payload_json"))
    return result


def get_opportunity_queue(database_path):
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_events.opportunity_id, opportunity_events.event_id,
                   opportunity_events.event_type, opportunity_events.event_timestamp,
                   opportunity_events.owner, opportunity_events.account,
                   opportunity_events.region, opportunity_events.customer_brief,
                   opportunity_events.source, opportunity_events.segment,
                   opportunity_events.regulator, opportunity_events.systems_json,
                   opportunity_events.expected_timeline,
                   opportunity_events.success_criteria_json, opportunity_events.received_at,
                   (
                       SELECT plans.id
                       FROM plans
                       JOIN briefs ON briefs.id = plans.brief_id
                       WHERE briefs.opportunity_id = opportunity_events.opportunity_id
                       ORDER BY plans.created_at DESC, plans.id DESC
                       LIMIT 1
                   ) AS latest_plan_id,
                   (
                       SELECT plans.status
                       FROM plans
                       JOIN briefs ON briefs.id = plans.brief_id
                       WHERE briefs.opportunity_id = opportunity_events.opportunity_id
                       ORDER BY plans.created_at DESC, plans.id DESC
                       LIMIT 1
                   ) AS latest_plan_status,
                   (
                       SELECT MIN(plans.created_at)
                       FROM plans
                       JOIN briefs ON briefs.id = plans.brief_id
                       WHERE briefs.opportunity_id = opportunity_events.opportunity_id
                   ) AS first_pickup_at
            FROM opportunity_events
            ORDER BY opportunity_events.event_timestamp DESC,
                     opportunity_events.opportunity_id
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
                SELECT event_timestamp, owner, account, region, customer_brief
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
                or brief_text.strip() != source_event["customer_brief"]
            ):
                raise ValueError(
                    "Opportunity account, owner, region, event timestamp, and brief "
                    "must match the selected source event."
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
            "SELECT event_timestamp, opportunity_id FROM briefs WHERE id = ?",
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
        for evidence in plan.get("evidence", []):
            if evidence.get("type") != "template" or not evidence.get("id"):
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO plan_templates (
                    plan_id, template_id, template_title
                )
                VALUES (?, ?, ?)
                """,
                (cursor.lastrowid, evidence["id"], evidence.get("title", evidence["id"])),
            )
        add_workflow_event(
            database_path,
            "plan_generated",
            opportunity_id=brief["opportunity_id"],
            brief_id=brief_id,
            plan_id=cursor.lastrowid,
            payload={
                "generation_mode": plan.get("generation_mode"),
                "generation_metadata": plan.get("generation_metadata"),
                "template_ids": _plan_template_ids(plan),
            },
            connection=connection,
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
                   plans.created_at AS first_pickup_at
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
                   plans.created_at AS first_pickup_at
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
            SELECT id, plan_id, opportunity_id, decision, reviewer_role, timestamp,
                   edit_details, rejection_reason, edited_plan_json,
                   template_ids_json, retained_template_ids_json
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
    retained_template_ids=None,
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
            """
            SELECT plans.id, plans.brief_id, plans.plan_json, briefs.opportunity_id
            FROM plans
            JOIN briefs ON briefs.id = plans.brief_id
            WHERE plans.id = ?
            """,
            (plan_id,),
        ).fetchone()
        if plan_row is None:
            raise ValueError(f"Plan {plan_id} does not exist.")
        current_plan = json.loads(plan_row["plan_json"])
        draft_template_ids = _plan_template_ids(current_plan)
        if decision == "edit" and not set(_plan_template_ids(edited_plan)).issubset(
            draft_template_ids
        ):
            raise ValueError("Edited plans cannot add templates absent from the draft.")
        if retained_template_ids is None:
            retained_template_ids = (
                _plan_template_ids(edited_plan)
                if decision == "edit"
                else draft_template_ids if decision == "accept" else []
            )
        if (
            not isinstance(retained_template_ids, list)
            or any(not isinstance(item, str) for item in retained_template_ids)
            or not set(retained_template_ids).issubset(draft_template_ids)
        ):
            raise ValueError("Retained template IDs must come from the current draft.")
        if decision == "edit" and not set(retained_template_ids).issubset(
            _plan_template_ids(edited_plan)
        ):
            raise ValueError(
                "Retained template IDs must remain in the edited plan evidence."
            )
        if decision == "reject" and retained_template_ids:
            raise ValueError("Rejected plans cannot retain template IDs.")

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
                plan_id, opportunity_id, decision, reviewer_role, timestamp,
                edit_details, rejection_reason, edited_plan_json,
                template_ids_json, retained_template_ids_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                plan_row["opportunity_id"],
                decision,
                reviewer_role.strip(),
                timestamp,
                edit_details.strip() if edit_details else None,
                rejection_reason.strip() if rejection_reason else None,
                json.dumps(edited_plan) if decision == "edit" else None,
                json.dumps(draft_template_ids),
                json.dumps(retained_template_ids),
            ),
        )
        add_workflow_event(
            database_path,
            "review_decision_recorded",
            opportunity_id=plan_row["opportunity_id"],
            brief_id=plan_row["brief_id"],
            plan_id=plan_id,
            payload={
                "decision": decision,
                "reviewer_role": reviewer_role.strip(),
                "decision_id": cursor.lastrowid,
                "template_ids": draft_template_ids,
                "retained_template_ids": retained_template_ids,
            },
            connection=connection,
        )
        return cursor.lastrowid
