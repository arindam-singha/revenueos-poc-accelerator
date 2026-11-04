import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from database import (
    get_opportunity_sla_triggers,
    ingest_opportunity_event,
    initialize_database,
    sync_opportunity_sla,
    save_brief,
    save_plan,
)


class CrmIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "revenueos.db"
        initialize_database(self.database_path)
        self.event = {
            "event_id": "crm-event-001",
            "event_type": "opportunity_won",
            "timestamp": "2026-10-26T08:30:00Z",
            "source": "test_crm",
            "owner": "Jordan Lee",
            "account": "Northwind Bank",
            "opportunity_id": "OPP-CRM-001",
            "region": "US",
            "segment": "Community banking",
            "regulator": "OCC",
            "customer_brief": "Automate onboarding document review.",
            "systems": ["Core banking", "DMS"],
            "expected_timeline": "6 weeks",
            "success_criteria": ["Reduce review time", "Keep audit trail"],
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_crm_event_persists_metadata_and_is_idempotent(self):
        first = ingest_opportunity_event(self.database_path, self.event)
        second = ingest_opportunity_event(self.database_path, self.event)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate"])
        from database import get_opportunity_events
        rows = get_opportunity_events(self.database_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "test_crm")
        self.assertEqual(rows[0]["regulator"], "OCC")

    def test_opportunity_id_collision_with_new_event_id_is_rejected(self):
        ingest_opportunity_event(self.database_path, self.event)
        changed = dict(self.event, event_id="crm-event-002")
        with self.assertRaises(ValueError):
            ingest_opportunity_event(self.database_path, changed)

    def test_two_business_day_breach_triggers_once_without_plan(self):
        ingest_opportunity_event(self.database_path, self.event)
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        created = sync_opportunity_sla(self.database_path, now=now)
        repeated = sync_opportunity_sla(self.database_path, now=now)
        self.assertEqual(len(created), 1)
        self.assertEqual(repeated, [])
        triggers = get_opportunity_sla_triggers(self.database_path)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["owner"], "Jordan Lee")
        self.assertEqual(triggers[0]["payload"]["draft"]["type"], "intake_brief")
        self.assertEqual(triggers[0]["payload"]["draft"]["status"], "poc_plan_not_started")


    def test_existing_breach_trigger_is_enriched_when_late_plan_is_created(self):
        ingest_opportunity_event(self.database_path, self.event)
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        sync_opportunity_sla(self.database_path, now=now)
        brief_id = save_brief(
            self.database_path,
            company="Northwind Bank",
            industry="Financial services",
            brief_text="Automate onboarding document review.",
            event_timestamp=self.event["timestamp"],
            owner="Jordan Lee",
            opportunity_id=self.event["opportunity_id"],
            region="US",
        )
        save_plan(self.database_path, brief_id, {"title": "Late POC draft", "evidence": []})
        sync_opportunity_sla(self.database_path, now=now)
        trigger = get_opportunity_sla_triggers(self.database_path)[0]
        self.assertEqual(trigger["payload"]["draft"]["title"], "Late POC draft")

    def test_invalid_event_timestamp_is_rejected(self):
        invalid = dict(self.event, timestamp="not-a-date")
        with self.assertRaises(ValueError):
            ingest_opportunity_event(self.database_path, invalid)


if __name__ == "__main__":
    unittest.main()

class CrmApiTests(unittest.TestCase):
    def test_webhook_endpoint_accepts_and_deduplicates_event(self):
        from fastapi.testclient import TestClient
        import crm_ingestion

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "api.db"
            event = {
                "event_id": "api-event-001", "event_type": "opportunity_won",
                "timestamp": "2026-10-26T08:30:00Z", "source": "crm-test",
                "owner": "Taylor Smith", "account": "API Bank",
                "opportunity_id": "OPP-API-001", "region": "US",
                "customer_brief": "Improve account onboarding review.",
                "systems": ["CRM"], "success_criteria": ["Shorter review time"],
            }
            with patch.object(crm_ingestion, "DATABASE_PATH", db_path), patch.dict("os.environ", {"CRM_WEBHOOK_TOKEN": "test-secret"}):
                with TestClient(crm_ingestion.app) as client:
                    unauthorized = client.post("/api/v1/events/opportunity-won", json=event)
                    self.assertEqual(unauthorized.status_code, 401)
                    first = client.post("/api/v1/events/opportunity-won", json=event, headers={"X-RevenueOS-Token": "test-secret"})
                    second = client.post("/api/v1/events/opportunity-won", json=event, headers={"X-RevenueOS-Token": "test-secret"})
                    self.assertEqual(first.status_code, 202)
                    self.assertTrue(first.json()["created"])
                    self.assertTrue(second.json()["duplicate"])
