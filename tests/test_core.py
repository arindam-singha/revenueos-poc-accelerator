import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import generate_plan
from database import (
    get_plan,
    get_recent_plans,
    get_review_decisions,
    initialize_database,
)
from retrieval import retrieve_context
from sla_monitor import get_sla_status
from workflow import create_plan, review_plan


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"


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
                api_key="",
            )
            self.assertGreater(plan_id, 0)
            self.assertIn("lead routing", plan["executive_summary"])
            saved = get_recent_plans(database_path)
            self.assertEqual(1, len(saved))
            self.assertEqual(plan_id, saved[0]["id"])
            self.assertEqual("Acme", saved[0]["company"])
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
    def test_statuses_include_complete_at_risk_and_breached(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = now.isoformat()
        self.assertEqual(
            "Complete",
            get_sla_status(created, (now - timedelta(minutes=1)).isoformat(), "complete", now),
        )
        self.assertEqual(
            "At risk · 30m left",
            get_sla_status(created, (now + timedelta(minutes=30)).isoformat(), now=now),
        )
        self.assertEqual(
            "Breached",
            get_sla_status(created, (now - timedelta(minutes=1)).isoformat(), now=now),
        )


if __name__ == "__main__":
    unittest.main()
