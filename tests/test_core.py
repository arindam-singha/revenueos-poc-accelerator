import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent import generate_plan
from database import (
    get_plan,
    get_recent_plans,
    get_review_decisions,
    get_sla_triggers,
    initialize_database,
    sync_sla_triggers,
)
from retrieval import retrieve_context
from sla_monitor import (
    get_business_day_age,
    get_sla_deadlines,
    get_sla_status,
)
from workflow import create_plan, review_plan


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"
EVENT_TIMESTAMP = "2026-10-26T08:30:00Z"


class RetrievalTests(unittest.TestCase):
    def test_retrieves_relevant_template_and_example(self):
        results = retrieve_context(
            "CRM data quality forecasting pipeline",
            DATA_DIR,
            limit=3,
        )
        self.assertTrue(results)
        self.assertEqual("template-crm-data", results[0]["id"])
        self.assertIn("score", results[0])

    def test_empty_query_returns_no_results(self):
        self.assertEqual([], retrieve_context("the and to", DATA_DIR))


class AgentTests(unittest.TestCase):
    def test_no_key_uses_complete_deterministic_plan(self):
        plan = generate_plan(
            company="Acme",
            industry="B2B SaaS",
            brief="We need CRM data quality and pipeline forecasting.",
            evidence=[],
            api_key="",
        )
        self.assertEqual("deterministic_fallback", plan["generation_mode"])
        self.assertTrue(plan["phases"])
        self.assertTrue(plan["success_metrics"])
        self.assertTrue(any("CRM" in item for item in plan["recommended_scope"]))

    def test_template_content_changes_substantive_plan_fields(self):
        brief = "We need to improve customer onboarding and review identity documents."
        kyc_template = {
            "id": "kyc",
            "type": "template",
            "title": "UK KYC intake",
            "reason": "Matched: onboarding, identity",
            "score": 0.8,
            "content": {
                "title": "UK KYC intake",
                "region": "UK",
                "use_case": "KYC document verification",
                "description": "Extract identity fields and route low-confidence cases for analyst review.",
                "required_capabilities": ["Identity-document samples"],
                "reusable_components": ["Document verification queue"],
            },
        }
        claims_template = {
            "id": "claims",
            "type": "template",
            "title": "India claims intake",
            "reason": "Matched: customer, documents",
            "score": 0.6,
            "content": {
                "title": "India claims intake",
                "region": "India",
                "use_case": "Insurance claims triage",
                "description": "Classify claim attachments and route incomplete claims to handlers.",
                "required_capabilities": ["Approved motor-claim samples"],
                "reusable_components": ["Claim attachment classifier"],
            },
        }

        kyc_plan = generate_plan(
            "Acme", "Financial services", brief, [kyc_template], api_key=""
        )
        claims_plan = generate_plan(
            "Acme", "Financial services", brief, [claims_template], api_key=""
        )

        self.assertNotEqual(kyc_plan["proposed_solution"], claims_plan["proposed_solution"])
        self.assertNotEqual(
            kyc_plan["required_data_integrations"],
            claims_plan["required_data_integrations"],
        )
        self.assertNotEqual(
            kyc_plan["phases"][1]["deliverable"],
            claims_plan["phases"][1]["deliverable"],
        )
        self.assertEqual(0.8, kyc_plan["evidence"][0]["score"])
        self.assertEqual("Matched: onboarding, identity", kyc_plan["evidence"][0]["reason"])
        self.assertIn("UK", kyc_plan["proposed_solution"])
        self.assertIn("India", claims_plan["proposed_solution"])

    def test_no_template_match_is_explicit_in_generic_plan(self):
        plan = generate_plan(
            company="Acme",
            industry="Financial services",
            brief="A bespoke workflow is needed.",
            evidence=[],
            api_key="",
        )

        self.assertIn("No suitable reusable template was identified", plan["proposed_solution"])
        self.assertTrue(
            any(
                "No suitable reusable template was identified" in assumption
                for assumption in plan["assumptions"]
            )
        )


class WorkflowTests(unittest.TestCase):
    def test_creates_persists_and_lists_plan(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "test.sqlite3"
            initialize_database(database_path)
            plan, plan_id = create_plan(
                company="Acme",
                industry="B2B SaaS",
                brief="We need to improve CRM data and automate lead routing.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=EVENT_TIMESTAMP,
                owner="Avery Morgan",
                opportunity_id="opp-test",
                region="US",
                api_key="",
            )
            self.assertGreater(plan_id, 0)
            self.assertIn("lead routing", plan["executive_summary"])
            saved = get_recent_plans(database_path)
            self.assertEqual(1, len(saved))
            self.assertEqual(plan_id, saved[0]["id"])
            self.assertEqual("Acme", saved[0]["company"])
            self.assertEqual("Avery Morgan", saved[0]["owner"])
            self.assertEqual("opp-test", saved[0]["opportunity_id"])
            self.assertEqual(EVENT_TIMESTAMP, saved[0]["event_timestamp"])
            self.assertEqual(plan["title"], json.loads(saved[0]["plan_json"])["title"])
            self.assertGreater(
                datetime.fromisoformat(saved[0]["sla_due_at"]),
                datetime.fromisoformat(saved[0]["created_at"]),
            )

    def test_accept_edit_and_reject_decisions_persist_with_plan_updates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "review.sqlite3"
            plan, plan_id = create_plan(
                company="Acme",
                industry="B2B SaaS",
                brief="We need to improve CRM data and automate lead routing.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=EVENT_TIMESTAMP,
                owner="Avery Morgan",
                opportunity_id="opp-test",
                region="US",
                api_key="",
            )

            review_plan(database_path, plan_id, "accept")
            accepted = get_plan(database_path, plan_id)
            self.assertEqual("accepted", accepted["status"])

            edited_plan = dict(plan)
            edited_plan["title"] = "Acme — reviewed POC plan"
            edited_plan["executive_summary"] = "Updated after architect review."
            review_plan(
                database_path,
                plan_id,
                "edit",
                edited_plan=edited_plan,
                edit_details="Clarified the executive summary.",
            )
            edited = get_plan(database_path, plan_id)
            self.assertEqual("edited", edited["status"])
            self.assertEqual(edited_plan["title"], edited["title"])
            self.assertEqual(
                "Updated after architect review.",
                edited["plan"]["executive_summary"],
            )

            review_plan(
                database_path,
                plan_id,
                "reject",
                rejection_reason="The integration scope needs further validation.",
            )
            rejected = get_plan(database_path, plan_id)
            decisions = get_review_decisions(database_path, plan_id)
            self.assertEqual("rejected", rejected["status"])
            self.assertEqual(["reject", "edit", "accept"], [
                decision["decision"] for decision in decisions
            ])
            self.assertTrue(
                all(
                    decision["reviewer_role"] == "US Solution Architect"
                    for decision in decisions
                )
            )
            self.assertTrue(all(decision["timestamp"] for decision in decisions))
            self.assertEqual(
                "The integration scope needs further validation.",
                decisions[0]["rejection_reason"],
            )
            self.assertEqual(
                "Clarified the executive summary.",
                decisions[1]["edit_details"],
            )

    def test_invalid_plan_or_decision_does_not_write_review_event(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "invalid-review.sqlite3"
            plan, plan_id = create_plan(
                company="Acme",
                industry="B2B SaaS",
                brief="We need to improve CRM data.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=EVENT_TIMESTAMP,
                owner="Avery Morgan",
                opportunity_id="opp-test",
                region="US",
                api_key="",
            )
            with self.assertRaisesRegex(ValueError, "does not exist"):
                review_plan(database_path, plan_id + 100, "accept")
            with self.assertRaisesRegex(ValueError, "edited plan with a title"):
                review_plan(
                    database_path,
                    plan_id,
                    "edit",
                    edited_plan={},
                )
            with self.assertRaisesRegex(ValueError, "rejection reason"):
                review_plan(database_path, plan_id, "reject", rejection_reason=" ")

            self.assertEqual("open", get_plan(database_path, plan_id)["status"])
            self.assertEqual([], get_review_decisions(database_path, plan_id))


class SlaTests(unittest.TestCase):
    def test_deadlines_exclude_weekends_in_us_eastern(self):
        amber_at, due_at = get_sla_deadlines("2026-10-23T15:00:00-04:00")
        self.assertEqual(datetime.fromisoformat("2026-10-26T15:00:00-04:00"), amber_at)
        self.assertEqual(datetime.fromisoformat("2026-10-27T15:00:00-04:00"), due_at)

        weekend_amber, weekend_due = get_sla_deadlines("2026-10-24T11:00:00-04:00")
        self.assertEqual(datetime.fromisoformat("2026-10-26T11:00:00-04:00"), weekend_amber)
        self.assertEqual(datetime.fromisoformat("2026-10-27T11:00:00-04:00"), weekend_due)

    def test_deadline_preserves_eastern_time_across_daylight_saving_change(self):
        amber_at, due_at = get_sla_deadlines("2026-10-30T15:00:00-04:00")
        self.assertEqual(datetime.fromisoformat("2026-11-02T15:00:00-05:00"), amber_at)
        self.assertEqual(datetime.fromisoformat("2026-11-03T15:00:00-05:00"), due_at)

    def test_green_amber_red_and_business_day_age(self):
        event_timestamp = "2026-10-23T15:00:00-04:00"
        self.assertEqual(
            "Green",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-26T14:59:00-04:00"),
            ),
        )
        self.assertEqual(
            "Amber",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-26T15:00:00-04:00"),
            ),
        )
        self.assertEqual(
            "Red",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-27T15:00:00-04:00"),
            ),
        )
        self.assertEqual(
            "Green",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-28T12:00:00-04:00"),
                first_pickup_at="2026-10-26T16:00:00-04:00",
            ),
        )
        self.assertEqual(
            "Red",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-28T12:00:00-04:00"),
                first_pickup_at="2026-10-28T10:00:00-04:00",
            ),
        )
        self.assertEqual(
            1,
            get_business_day_age(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-26T12:00:00-04:00"),
            ),
        )

    def test_plan_due_time_comes_from_original_event_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "event-deadline.sqlite3"
            _, plan_id = create_plan(
                company="Acme",
                industry="Financial services",
                brief="Test brief.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp="2026-10-23T15:00:00-04:00",
                owner="Avery Morgan",
                api_key="",
            )
            saved = get_plan(database_path, plan_id)
            self.assertEqual(
                datetime.fromisoformat("2026-10-27T15:00:00-04:00"),
                datetime.fromisoformat(saved["sla_due_at"]),
            )
            self.assertEqual("2026-10-23T15:00:00-04:00", saved["event_timestamp"])

    def test_breach_trigger_is_persisted_once_with_draft_reference(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "breach.sqlite3"
            _, plan_id = create_plan(
                company="Acme",
                industry="Financial services",
                brief="Test brief.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp="2026-10-23T15:00:00-04:00",
                owner="Avery Morgan",
                opportunity_id="opp-breach",
                api_key="",
            )
            breach_time = datetime.fromisoformat("2026-10-27T15:00:00-04:00")
            sync_sla_triggers(database_path, now=breach_time)
            sync_sla_triggers(database_path, now=datetime.fromisoformat("2026-10-28T10:00:00-04:00"))

            triggers = get_sla_triggers(database_path)
            self.assertEqual(1, len(triggers))
            self.assertEqual(plan_id, triggers[0]["plan_id"])
            self.assertEqual("Avery Morgan", triggers[0]["owner"])
            self.assertEqual("Red", triggers[0]["status"])
            self.assertEqual(
                datetime.fromisoformat("2026-10-27T15:00:00-04:00"),
                datetime.fromisoformat(triggers[0]["breach_timestamp"]),
            )
            self.assertEqual(f"plans:{plan_id}", triggers[0]["current_draft_reference"])
            self.assertIn("title", json.loads(triggers[0]["draft_json"]))

    def test_missing_original_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "original opportunity event timestamp"):
            get_sla_deadlines("")


if __name__ == "__main__":
    unittest.main()
