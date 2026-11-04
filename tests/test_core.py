import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent import (
    AgentError,
    OLLAMA_CONNECT_TIMEOUT_SECONDS,
    OLLAMA_INFERENCE_TIMEOUT_SECONDS,
    OLLAMA_PLAN_TIMEOUT_SECONDS,
    _ollama_exchange,
    _ensure_business_problem_account_reference,
    generate_plan,
)
from database import (
    get_workflow_events,
    get_opportunity_events,
    get_opportunity_queue,
    get_plan,
    get_recent_plans,
    get_review_decisions,
    get_sla_triggers,
    get_template_ranking_feedback,
    get_template_reuse_metrics,
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


def _ollama_evidence():
    return [
        item
        for item in retrieve_context(
            "UK KYC onboarding identity documents verification",
            DATA_DIR,
            limit=20,
        )
        if item["type"] == "template" and item["id"] == "template-uk-kyc"
    ]


def _valid_ollama_plan(company="Acme", opportunity_context=None, evidence=None):
    template = next(
        item["content"]
        for item in (evidence or _ollama_evidence())
        if item.get("type") == "template"
    )
    opportunity_context = opportunity_context or {}
    return {
        "title": f"{company} POC",
        "customer_context": {
            "account": company,
            "industry": "Financial services",
            "opportunity_id": opportunity_context.get("opportunity_id"),
            "region": opportunity_context.get("region"),
            "owner": opportunity_context.get("owner"),
        },
        "facts_from_brief": [
            {
                "fact": "The workflow includes identity-document review.",
                "evidence_quote": "identity document review",
            }
        ],
        "business_problem": f"{company} has manual identity document review.",
        "objectives": ["Measure onboarding review time."],
        "executive_summary": f"Test assisted onboarding for {company}.",
        "proposed_solution": "Prototype document extraction with analyst verification.",
        "architecture_outline": "Approved documents -> extraction -> analyst review -> audit record.",
        "recommended_scope": ["One customer-approved onboarding cohort."],
        "exclusions": ["No production deployment or automatic identity decisions."],
        "deliverables": ["Configured review flow and evaluation readout."],
        "phases": [
            {
                "name": "Discovery and pilot",
                "duration": "Estimate: 10 business days",
                "tasks": ["Confirm sample access.", "Configure and evaluate document review."],
                "deliverable": "Tested review workflow",
                "acceptance_criteria": ["Measure field extraction against an approved labeled sample."],
            }
        ],
        "required_data_integrations": ["Approved identity-document samples."],
        "data_security_considerations": ["Confirm access, retention, privacy, and processing location."],
        "human_review_requirements": ["Analysts verify low-confidence and exception cases."],
        "success_metrics": ["Measure field-level accuracy and review turnaround against agreed baselines."],
        "evaluation_strategy": ["Use a representative approved sample and reviewer-labeled expected fields."],
        "acceptance_criteria": ["Customer agrees numeric accuracy and turnaround thresholds before the pilot."],
        "risks": ["Unapproved document access may delay testing."],
        "assumptions": ["The customer will approve sample documents."],
        "dependencies": ["Named customer data owner and access approval."],
        "open_questions": ["Which document types may be used?"],
        "timeline_estimate": {
            "duration": "10 business days",
            "qualification": "Estimate only; confirm after access and integration discovery.",
        },
        "exit_criteria": ["Compare pilot results with approved thresholds and record owner sign-off."],
        "next_steps": ["Confirm sample approval and the business baseline."],
        "template_reuse": [
            {
                "template_id": template["id"],
                "title": template["title"],
                "source_region": template["region"],
                "relevance_explanation": "It covers onboarding document extraction and analyst verification.",
                "reusable_components": [template["reusable_components"][0]],
                "adaptations": ["Confirm the permitted document types and customer policy."],
                "limitations": ["Template does not establish this customer's data access or policy approvals."],
            }
        ],
        "delivery_handoff": {
            "business_problem": f"{company} manually reviews onboarding documents.",
            "desired_outcome": "Reduce review effort while maintaining human decisions.",
            "in_scope": ["Document extraction and analyst review."],
            "out_of_scope": ["Automated identity approval."],
            "functional_requirements": ["Extract agreed fields with source references."],
            "expected_workflow": ["Load approved sample.", "Review extracted fields.", "Record exceptions."],
            "proposed_components": ["Document intake", "Analyst review queue"],
            "integration_boundaries": ["Prototype uses approved sample only until interfaces are confirmed."],
            "data_inputs": ["Customer-approved identity samples."],
            "data_outputs": ["Extracted fields and reviewer disposition."],
            "dependencies": ["Data owner and access approval."],
            "security_privacy_access_audit": ["Confirm controls, retention, and audit requirements."],
            "milestones": ["Discovery", "Prototype", "Evaluation"],
            "engineering_tasks": ["Define input schema.", "Implement extraction and exception routing."],
            "deliverables": ["Review workflow and test readout."],
            "acceptance_criteria": ["Meet customer-approved extraction and review-time thresholds."],
            "evaluation_metrics": ["Field accuracy", "Exception rate", "Review turnaround"],
            "risks_blockers": ["Data approval delay."],
            "assumptions": ["Approved sample data will be provided."],
            "customer_questions": ["Which fields and documents are in scope?"],
            "solution_architect_confirmations": ["Confirm customer scope and policy boundaries."],
            "delivery_lead_confirmations": ["Confirm engineering owner and estimate."],
        },
    }


def _mock_ollama_response(plan_or_content, done_reason="stop", done=True):
    content = (
        plan_or_content
        if isinstance(plan_or_content, str)
        else json.dumps(plan_or_content)
    )
    result = {
        "message": {"role": "assistant", "content": content},
        "done": done,
        "prompt_eval_count": 87,
        "eval_count": 512,
        "total_duration": 123000,
    }
    if done_reason is not None:
        result["done_reason"] = done_reason
    return result


def _mock_ollama_plan_responses(plan):
    plan_section = {
        key: value for key, value in plan.items() if key != "delivery_handoff"
    }
    return [
        _mock_ollama_response(plan_section),
        _mock_ollama_response({"delivery_handoff": plan["delivery_handoff"]}),
    ]


class _FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _FakeHTTPResponse:
    def __init__(self, payload, status=200, read_error=None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status
        self.read_error = read_error

    def read(self):
        if self.read_error:
            raise self.read_error
        return self.payload


class _FakeHTTPConnection:
    instances = []
    connect_error = None
    response_error = None
    response = None

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = _FakeSocket()
        self.request_args = None
        self.closed = False
        self.__class__.instances.append(self)

    def connect(self):
        if self.__class__.connect_error:
            raise self.__class__.connect_error

    def request(self, method, path, body, headers):
        self.request_args = (method, path, body, headers)

    def getresponse(self):
        if self.__class__.response_error:
            raise self.__class__.response_error
        return self.__class__.response

    def close(self):
        self.closed = True


class RetrievalTests(unittest.TestCase):
    def test_template_library_has_financial_services_coverage_across_regions(self):
        templates = json.loads(
            (DATA_DIR / "templates.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(templates), 8)
        self.assertGreaterEqual(
            len({template["region"] for template in templates}),
            2,
        )
        self.assertTrue(
            all(template.get("use_case") for template in templates)
        )

    def test_retrieves_relevant_template_and_example(self):
        results = retrieve_context(
            "CRM data quality forecasting pipeline",
            DATA_DIR,
            limit=3,
        )
        self.assertTrue(results)
        self.assertEqual("template-crm-data", results[0]["id"])
        self.assertGreater(results[0]["score"], 0)
        self.assertLessEqual(results[0]["score"], 1)
        self.assertTrue(results[0]["reason"].startswith("Matched: "))
        self.assertTrue(results[0]["reason"].removeprefix("Matched: ").strip())

    def test_empty_query_returns_no_results(self):
        self.assertEqual([], retrieve_context("the and to", DATA_DIR))


class AgentTests(unittest.TestCase):
    def setUp(self):
        _FakeHTTPConnection.instances = []
        _FakeHTTPConnection.connect_error = None
        _FakeHTTPConnection.response_error = None
        _FakeHTTPConnection.response = None

    def test_business_problem_is_grounded_to_selected_account_without_model_retry(self):
        plan = {"business_problem": "Manual onboarding reviews delay customer activation."}
        result = _ensure_business_problem_account_reference(plan, "Northstar Community Bank")
        self.assertIs(result, plan)
        self.assertEqual(
            "Northstar Community Bank: Manual onboarding reviews delay customer activation.",
            result["business_problem"],
        )

    def test_business_problem_does_not_duplicate_selected_account(self):
        plan = {"business_problem": "Northstar Community Bank has slow onboarding reviews."}
        result = _ensure_business_problem_account_reference(plan, "Northstar Community Bank")
        self.assertEqual(
            "Northstar Community Bank has slow onboarding reviews.",
            result["business_problem"],
        )

    def test_explicit_demo_fixture_generates_template_driven_plan(self):
        with patch.dict("os.environ", {}, clear=True):
            plan = generate_plan(
                company="Acme",
                industry="B2B SaaS",
                brief="We need CRM data quality and pipeline forecasting.",
                evidence=[],
                mode="demo",
            )
        self.assertEqual("demo_fixture", plan["generation_mode"])
        self.assertTrue(plan["phases"])
        self.assertTrue(plan["success_metrics"])
        self.assertTrue(any("CRM" in item for item in plan["recommended_scope"]))

    def test_unmatched_brief_uses_explicit_generic_offline_plan(self):
        brief = "We need an unusual bespoke workflow for a quartz telescope."
        with tempfile.TemporaryDirectory() as temporary_directory:
            isolated_data_dir = Path(temporary_directory)
            (isolated_data_dir / "templates.json").write_text("[]", encoding="utf-8")
            (isolated_data_dir / "briefs.json").write_text("[]", encoding="utf-8")
            evidence = retrieve_context(brief, isolated_data_dir)
            self.assertEqual([], evidence)

            with patch.dict("os.environ", {}, clear=True):
                plan = generate_plan(
                    company="Acme",
                    industry="Financial services",
                    brief=brief,
                    evidence=evidence,
                    mode="demo",
                )

        self.assertEqual("demo_fixture", plan["generation_mode"])
        self.assertIn(
            "No suitable reusable template was identified",
            plan["proposed_solution"],
        )
        self.assertTrue(plan["phases"])
        self.assertTrue(plan["required_data_integrations"])

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
            "Acme", "Financial services", brief, [kyc_template], mode="demo"
        )
        claims_plan = generate_plan(
            "Acme", "Financial services", brief, [claims_template], mode="demo"
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
            mode="demo",
        )

        self.assertIn("No suitable reusable template was identified", plan["proposed_solution"])
        self.assertTrue(
            any(
                "No suitable reusable template was identified" in assumption
                for assumption in plan["assumptions"]
            )
        )

    def test_ollama_response_is_parsed_with_retrieved_template_context(self):
        evidence = _ollama_evidence()
        brief = "Slow customer onboarding needs identity document review."
        context = {
            "opportunity_id": "opp-test",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        response_plan = _valid_ollama_plan("Acme", context, evidence)
        with self.assertLogs("agent", level="INFO") as generation_logs:
            with patch(
                "agent._ollama_exchange",
                side_effect=_mock_ollama_plan_responses(response_plan),
            ) as mocked_exchange:
                plan = generate_plan(
                    "Acme",
                    "Financial services",
                    brief,
                    evidence,
                    mode="ollama",
                    model="qwen-test",
                    base_url="http://localhost:11434",
                    opportunity_context=context,
                )

        self.assertEqual("ollama", plan["generation_mode"])
        self.assertEqual("qwen-test", plan["generation_metadata"]["model"])
        self.assertEqual(context["opportunity_id"], plan["customer_context"]["opportunity_id"])
        self.assertEqual(
            "http://localhost:11434/api/chat",
            mocked_exchange.call_args.args[0],
        )
        request_body = json.loads(
            mocked_exchange.call_args_list[0].args[1].decode("utf-8")
        )
        self.assertEqual("qwen-test", request_body["model"])
        self.assertFalse(request_body["stream"])
        self.assertEqual("json", request_body["format"])
        self.assertFalse(request_body["think"])
        self.assertEqual(2048, request_body["options"]["num_predict"])
        prompt = json.loads(request_body["messages"][1]["content"])
        self.assertEqual(brief, prompt["customer_brief"])
        self.assertEqual("template-uk-kyc", prompt["retrieved_templates"][0]["id"])
        self.assertNotIn("delivery_handoff", prompt["required_json_shape"])
        handoff_request = json.loads(
            mocked_exchange.call_args_list[1].args[1].decode("utf-8")
        )
        handoff_prompt = json.loads(handoff_request["messages"][1]["content"])
        self.assertEqual(
            ["string"],
            handoff_prompt["required_json_shape"]["delivery_handoff"]["expected_workflow"],
        )
        self.assertIn("2-3 expected_workflow steps", handoff_prompt["instructions"])
        self.assertIn("generation_timings", plan["generation_metadata"])
        self.assertEqual(
            "stop", plan["generation_metadata"]["response_completion"]["done_reason"]
        )
        self.assertGreater(
            plan["generation_metadata"]["request_sizes_bytes"]["initial"],
            len(brief),
        )
        log_output = "\n".join(generation_logs.output)
        self.assertIn("poc_plan call completed in", log_output)
        self.assertIn("prompt_eval_count=87", log_output)
        self.assertIn("eval_count=512", log_output)
        self.assertIn("total_duration=123000", log_output)
        self.assertNotIn(brief, log_output)

    def test_unverifiable_brief_quote_is_replaced_with_exact_excerpt_without_retry(self):
        evidence = _ollama_evidence()
        brief = "Slow customer onboarding needs identity document review and audit logging."
        response_plan = _valid_ollama_plan("Acme", evidence=evidence)
        response_plan["facts_from_brief"] = [
            {
                "fact": "Customer needs instant automated approvals.",
                "evidence_quote": "customer needs instant automated approvals",
            }
        ]
        with patch(
            "agent._ollama_exchange",
            side_effect=_mock_ollama_plan_responses(response_plan),
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                brief,
                evidence,
                mode="ollama",
                opportunity_context={},
            )

        self.assertEqual(2, mocked_exchange.call_count)
        fact = plan["facts_from_brief"][0]
        self.assertIn(fact["evidence_quote"], brief)
        self.assertTrue(fact["fact"].startswith("Customer brief states:"))
        self.assertFalse(plan["generation_metadata"]["validation_repair_used"])

    def test_missing_handoff_workflow_is_repaired_not_derived(self):
        evidence = _ollama_evidence()
        context = {
            "opportunity_id": "opp-test",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        response_plan = _valid_ollama_plan("Acme", context, evidence)
        del response_plan["delivery_handoff"]["expected_workflow"]
        response_plan["phases"] = [
            {
                "name": "Discovery",
                "duration": "Estimate: 2 days",
                "tasks": ["Confirm approved sample access."],
                "deliverable": "Approved sample definition",
                "acceptance_criteria": ["Customer confirms sample scope."],
            }
        ]
        response_plan.pop("delivery_handoff")
        response_plan["delivery_handoff"] = _valid_ollama_plan(
            "Acme", context, evidence
        )["delivery_handoff"]
        response_plan["delivery_handoff"].pop("expected_workflow")
        repair = {
            "delivery_handoff": {
                "expected_workflow": ["Confirm approved sample access."]
            }
        }
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response(
                    {
                        key: value
                        for key, value in response_plan.items()
                        if key != "delivery_handoff"
                    }
                ),
                _mock_ollama_response(
                    {"delivery_handoff": response_plan["delivery_handoff"]}
                ),
                _mock_ollama_response(repair),
            ],
        ):
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
                opportunity_context=context,
            )

        self.assertEqual(
            repair["delivery_handoff"]["expected_workflow"],
            plan["delivery_handoff"]["expected_workflow"],
        )

    def test_missing_handoff_workflow_is_repaired_without_rewriting_plan(self):
        evidence = _ollama_evidence()
        context = {
            "opportunity_id": "opp-test",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        original = _valid_ollama_plan("Acme", context, evidence)
        del original["delivery_handoff"]["expected_workflow"]
        repair = {
            "delivery_handoff": {
                "expected_workflow": [
                    "Confirm approved sample access.",
                    "Evaluate analyst review results.",
                ]
            }
        }
        responses = _mock_ollama_plan_responses(original)
        responses.append(_mock_ollama_response(repair))
        with patch(
            "agent._ollama_exchange",
            side_effect=responses,
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
                opportunity_context=context,
            )

        self.assertEqual(
            repair["delivery_handoff"]["expected_workflow"],
            plan["delivery_handoff"]["expected_workflow"],
        )
        self.assertEqual(
            original["proposed_solution"],
            plan["proposed_solution"],
        )
        self.assertTrue(plan["generation_metadata"]["validation_repair_used"])
        repair_request = json.loads(
            mocked_exchange.call_args_list[2].args[1].decode("utf-8")
        )["messages"][1]["content"]
        repair_payload = json.loads(repair_request)
        self.assertEqual(
            {"delivery_handoff": {"expected_workflow": ["string"]}},
            repair_payload["required_shape"],
        )

    def test_invalid_field_repair_preserves_valid_generated_sections(self):
        evidence = _ollama_evidence()
        original = _valid_ollama_plan("Acme", evidence=evidence)
        original["architecture_outline"] = ""
        replacement = {"architecture_outline": "Approved documents flow to analyst review."}
        responses = [
            _mock_ollama_response(
                {
                    key: value
                    for key, value in original.items()
                    if key != "delivery_handoff"
                }
            ),
            _mock_ollama_response(replacement),
            _mock_ollama_response(
                {"delivery_handoff": original["delivery_handoff"]}
            ),
        ]
        with patch(
            "agent._ollama_exchange",
            side_effect=responses,
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
            )

        self.assertEqual(
            "Approved documents flow to analyst review.",
            plan["architecture_outline"],
        )
        self.assertEqual(original["proposed_solution"], plan["proposed_solution"])
        self.assertEqual(3, mocked_exchange.call_count)
        repair_timeout = mocked_exchange.call_args_list[1].args[2]
        self.assertGreater(repair_timeout, 0)
        self.assertLessEqual(repair_timeout, OLLAMA_PLAN_TIMEOUT_SECONDS)
        repair_payload = json.loads(
            json.loads(mocked_exchange.call_args_list[1].args[1].decode("utf-8"))["messages"][1]["content"]
        )
        self.assertEqual("architecture_outline", repair_payload["repair_path"])
        self.assertNotIn("required_json_shape", repair_payload)

    def test_string_handoff_workflow_is_rejected_and_repaired_to_array(self):
        evidence = _ollama_evidence()
        context = {
            "opportunity_id": "opp-test",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        response_plan = _valid_ollama_plan("Acme", context, evidence)
        response_plan["delivery_handoff"]["expected_workflow"] = (
            "Confirm sample; configure pilot; review results"
        )
        plan_section = {
            key: value
            for key, value in response_plan.items()
            if key != "delivery_handoff"
        }
        repair = {
            "delivery_handoff": {
                **response_plan["delivery_handoff"],
                "expected_workflow": [
                    "Confirm sample.",
                    "Configure pilot.",
                    "Review results.",
                ],
            }
        }
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response(plan_section),
                _mock_ollama_response(
                    {"delivery_handoff": response_plan["delivery_handoff"]}
                ),
                _mock_ollama_response(repair),
            ],
        ):
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
                opportunity_context=context,
            )

        self.assertEqual(
            repair["delivery_handoff"]["expected_workflow"],
            plan["delivery_handoff"]["expected_workflow"],
        )

    def test_ollama_connection_failure_and_timeout_are_reported(self):
        _FakeHTTPConnection.connect_error = socket.timeout("connect deadline")
        with patch("agent.http.client.HTTPConnection", _FakeHTTPConnection):
            with self.assertRaisesRegex(AgentError, "Timed out connecting"):
                _ollama_exchange("http://localhost:11434/api/chat", b"{}")
        self.assertEqual(OLLAMA_CONNECT_TIMEOUT_SECONDS, _FakeHTTPConnection.instances[0].timeout)

        _FakeHTTPConnection.connect_error = None
        _FakeHTTPConnection.response_error = None
        _FakeHTTPConnection.response = _FakeHTTPResponse(
            {"message": {"content": "late"}},
            read_error=socket.timeout("body read deadline"),
        )
        with patch("agent.http.client.HTTPConnection", _FakeHTTPConnection):
            with self.assertRaisesRegex(AgentError, "inference timed out") as error:
                _ollama_exchange("http://localhost:11434/api/chat", b"{}")
        self.assertIn("TCP connection was established", str(error.exception))
        self.assertNotIn("Could not connect", str(error.exception))
        self.assertGreaterEqual(
            _FakeHTTPConnection.instances[-1].sock.timeouts[0],
            OLLAMA_INFERENCE_TIMEOUT_SECONDS - 1,
        )

    def test_ollama_exchange_uses_separate_connection_and_inference_timeouts(self):
        _FakeHTTPConnection.response = _FakeHTTPResponse(
            {"message": {"content": "ok"}, "done": True}
        )
        with patch("agent.http.client.HTTPConnection", _FakeHTTPConnection):
            result = _ollama_exchange("http://localhost:11434/api/chat", b'{"test":true}')

        connection = _FakeHTTPConnection.instances[0]
        self.assertEqual(OLLAMA_CONNECT_TIMEOUT_SECONDS, connection.timeout)
        self.assertEqual("POST", connection.request_args[0])
        self.assertEqual("/api/chat", connection.request_args[1])
        self.assertEqual(b'{"test":true}', connection.request_args[2])
        self.assertGreaterEqual(
            connection.sock.timeouts[0],
            OLLAMA_INFERENCE_TIMEOUT_SECONDS - 1,
        )
        self.assertTrue(connection.closed)
        self.assertEqual({"message": {"content": "ok"}, "done": True}, result)

    def test_ollama_http_500_is_reported_as_server_error(self):
        _FakeHTTPConnection.response = _FakeHTTPResponse(
            {"error": "generation task cancelled"}, status=500
        )
        with patch("agent.http.client.HTTPConnection", _FakeHTTPConnection):
            with self.assertRaisesRegex(AgentError, "Ollama returned HTTP 500"):
                _ollama_exchange("http://localhost:11434/api/chat", b"{}")

    def test_incomplete_ollama_response_retries_once_with_higher_token_limit(self):
        response_plan = _valid_ollama_plan("Acme", evidence=_ollama_evidence())
        plan_section = {
            key: value
            for key, value in response_plan.items()
            if key != "delivery_handoff"
        }
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response("{", done_reason="length"),
                _mock_ollama_response(plan_section),
                _mock_ollama_response(
                    {"delivery_handoff": response_plan["delivery_handoff"]}
                ),
            ],
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow onboarding needs identity document review.",
                _ollama_evidence(),
                mode="ollama",
            )
        self.assertEqual("ollama", plan["generation_mode"])
        self.assertEqual(3, mocked_exchange.call_count)
        first_request = json.loads(mocked_exchange.call_args_list[0].args[1])
        retry_request = json.loads(mocked_exchange.call_args_list[1].args[1])
        self.assertEqual(2048, first_request["options"]["num_predict"])
        self.assertEqual(3072, retry_request["options"]["num_predict"])

    def test_incomplete_retry_is_not_parsed_or_accepted(self):
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response("{", done_reason="length"),
                _mock_ollama_response("{", done_reason="length"),
            ],
        ) as mocked_exchange:
            with self.assertRaisesRegex(AgentError, "retry incomplete"):
                generate_plan(
                    "Acme",
                    "Financial services",
                    "Slow onboarding needs identity document review.",
                    _ollama_evidence(),
                    mode="ollama",
                )
        self.assertEqual(2, mocked_exchange.call_count)

    def test_invalid_token_limits_fail_before_request(self):
        with patch.dict(
            "os.environ",
            {"OLLAMA_NUM_PREDICT": "2048", "OLLAMA_RETRY_NUM_PREDICT": "2048"},
        ):
            with patch("agent._ollama_exchange") as mocked_exchange:
                with self.assertRaisesRegex(AgentError, "must be greater"):
                    generate_plan(
                        "Acme",
                        "Financial services",
                        "Brief",
                        [],
                        mode="ollama",
                    )
        mocked_exchange.assert_not_called()

    def test_ollama_token_limits_are_configurable(self):
        response_plan = _valid_ollama_plan("Acme", evidence=_ollama_evidence())
        plan_section = {
            key: value
            for key, value in response_plan.items()
            if key != "delivery_handoff"
        }
        with patch.dict(
            "os.environ",
            {"OLLAMA_NUM_PREDICT": "1024", "OLLAMA_RETRY_NUM_PREDICT": "1536"},
        ):
            with patch(
                "agent._ollama_exchange",
                side_effect=[
                    _mock_ollama_response("{", done_reason="length"),
                    _mock_ollama_response(plan_section),
                    _mock_ollama_response(
                        {"delivery_handoff": response_plan["delivery_handoff"]}
                    ),
                ],
            ) as mocked_exchange:
                generate_plan(
                    "Acme",
                    "Financial services",
                    "Slow onboarding needs identity document review.",
                    _ollama_evidence(),
                    mode="ollama",
                )

        budgets = [
            json.loads(call.args[1])["options"]["num_predict"]
            for call in mocked_exchange.call_args_list
        ]
        self.assertEqual([1024, 1536, 1024], budgets)

    def test_default_mode_is_ollama_and_model_errors_do_not_fall_back(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "agent._ollama_exchange",
                side_effect=AgentError("Could not connect to Ollama: connection refused."),
            ):
                with self.assertRaisesRegex(AgentError, "Could not connect to Ollama"):
                    generate_plan("Acme", "Financial services", "Brief", [])

        model_error = {"error": "model not found"}
        with patch("agent._ollama_exchange", return_value=model_error):
            with self.assertRaisesRegex(AgentError, "model not found"):
                generate_plan(
                    "Acme", "Financial services", "Brief", [], mode="ollama"
                )

    def test_model_json_parser_escapes_raw_newlines_inside_string_values(self):
        from agent import _parse_model_json

        raw = '{"title":"Acme POC", "summary":"first line' + "\n" + 'second line"}'
        parsed = _parse_model_json(raw, "test")
        self.assertEqual("first line\nsecond line", parsed["summary"])

    def test_model_json_parser_preserves_structural_newlines(self):
        from agent import _parse_model_json

        raw = """{
  "title": "Acme POC",
  "items": ["one", "two"]
}"""
        parsed = _parse_model_json(raw, "test")
        self.assertEqual(["one", "two"], parsed["items"])

    def test_malformed_ollama_plan_json_gets_one_bounded_syntax_repair(self):
        evidence = _ollama_evidence()
        valid_plan = _valid_ollama_plan("Acme", evidence=evidence)
        plan_section = {
            key: value for key, value in valid_plan.items() if key != "delivery_handoff"
        }
        malformed = '{"title" "Acme POC", "business_problem": "test"}'
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response(malformed),
                _mock_ollama_response(plan_section),
                _mock_ollama_response({"delivery_handoff": valid_plan["delivery_handoff"]}),
            ],
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
            )
        self.assertEqual("Acme POC", plan["title"])
        self.assertEqual(3, mocked_exchange.call_count)
        repair_request = json.loads(mocked_exchange.call_args_list[1].args[1])
        self.assertIn("Repair malformed JSON", repair_request["messages"][0]["content"])
        repair_payload = json.loads(repair_request["messages"][1]["content"])
        self.assertEqual(malformed, repair_payload["malformed_response"])

    def test_missing_fields_and_fabricated_template_ids_are_rejected_after_repair(self):
        evidence = _ollama_evidence()
        incomplete = _valid_ollama_plan("Acme", evidence=evidence)
        incomplete.pop("architecture_outline")
        repair = {"architecture_outline": "Approved documents flow to analyst review."}
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response(
                    {
                        key: value
                        for key, value in incomplete.items()
                        if key != "delivery_handoff"
                    }
                ),
                _mock_ollama_response(repair),
                _mock_ollama_response(
                    {"delivery_handoff": incomplete["delivery_handoff"]}
                ),
            ],
        ) as mocked_exchange:
            plan = generate_plan(
                "Acme",
                "Financial services",
                "Slow customer onboarding needs identity document review.",
                evidence,
                mode="ollama",
            )
        self.assertEqual(repair["architecture_outline"], plan["architecture_outline"])
        self.assertEqual(3, mocked_exchange.call_count)

        fabricated = _valid_ollama_plan("Acme", evidence=evidence)
        fabricated["template_reuse"][0]["template_id"] = "unretrieved-template"
        with patch(
            "agent._ollama_exchange",
            side_effect=[
                _mock_ollama_response(
                    {
                        key: value
                        for key, value in fabricated.items()
                        if key != "delivery_handoff"
                    }
                ),
                _mock_ollama_response(
                    {
                        key: value
                        for key, value in fabricated.items()
                        if key != "delivery_handoff"
                    }
                ),
            ],
        ):
            with self.assertRaises(AgentError):
                generate_plan(
                    "Acme",
                    "Financial services",
                    "Slow customer onboarding needs identity document review.",
                    evidence,
                    mode="ollama",
                )


class WorkflowTests(unittest.TestCase):
    def test_mocked_ollama_workflow_persists_plan_handoff_metadata_and_events(self):
        context = {
            "opportunity_id": "ollama-opp-1",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        evidence = retrieve_context(
            "Financial services UK KYC onboarding identity document review.",
            DATA_DIR,
        )
        response_plan = _valid_ollama_plan("Northstar Bank", context, evidence)
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "ollama.sqlite3"
            with patch(
                "agent._ollama_exchange",
                side_effect=_mock_ollama_plan_responses(response_plan),
            ):
                plan, plan_id = create_plan(
                    company="Northstar Bank",
                    industry="Financial services",
                    brief="UK KYC onboarding identity document review.",
                    database_path=database_path,
                    data_dir=DATA_DIR,
                    event_timestamp=EVENT_TIMESTAMP,
                    owner=context["owner"],
                    opportunity_id=context["opportunity_id"],
                    region=context["region"],
                    mode="ollama",
                    model="qwen-test",
                )
            saved = get_plan(database_path, plan_id)
            self.assertEqual("ollama", saved["plan"]["generation_mode"])
            self.assertIn("engineering_tasks", saved["plan"]["delivery_handoff"])
            self.assertEqual("qwen-test", saved["plan"]["generation_metadata"]["model"])
            self.assertEqual(
                "ollama",
                plan["generation_metadata"]["provider"],
            )
            events = get_workflow_events(database_path, plan_id=plan_id)
            self.assertEqual(
                ["plan_generated"],
                [event["event_type"] for event in events],
            )
            opportunity_events = get_workflow_events(
                database_path,
                opportunity_id=context["opportunity_id"],
            )
            self.assertEqual(
                {"plan_generation_started", "plan_generated"},
                {event["event_type"] for event in opportunity_events},
            )
            self.assertEqual(context["opportunity_id"], events[0]["opportunity_id"])
            self.assertEqual(
                [
                    item["id"]
                    for item in plan["evidence"]
                    if item.get("type") == "template"
                ],
                events[0]["payload"]["template_ids"],
            )

    def test_ollama_failure_is_logged_without_saving_a_fake_plan(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "ollama-failure.sqlite3"
            with patch(
                "agent._ollama_exchange",
                side_effect=AgentError("Could not connect to Ollama: connection refused."),
            ):
                with self.assertRaises(AgentError):
                    create_plan(
                        company="Acme",
                        industry="Financial services",
                        brief="KYC identity onboarding.",
                        database_path=database_path,
                        data_dir=DATA_DIR,
                        event_timestamp=EVENT_TIMESTAMP,
                        owner="Alex Reviewer",
                        opportunity_id="ollama-failed-1",
                        region="UK",
                        mode="ollama",
                    )
            self.assertEqual([], get_recent_plans(database_path))
            events = get_workflow_events(
                database_path,
                opportunity_id="ollama-failed-1",
            )
            self.assertEqual(
                ["plan_generation_failed", "plan_generation_started"],
                [event["event_type"] for event in events],
            )
            self.assertIn("Could not connect to Ollama", events[0]["payload"]["error"])

    def test_valid_plan_stage_is_persisted_if_handoff_generation_fails(self):
        context = {
            "opportunity_id": "ollama-partial-1",
            "region": "UK",
            "owner": "Alex Reviewer",
        }
        evidence = retrieve_context(
            "Financial services UK KYC onboarding identity document review.",
            DATA_DIR,
        )
        response_plan = _valid_ollama_plan("Northstar Bank", context, evidence)
        plan_section = {
            key: value
            for key, value in response_plan.items()
            if key != "delivery_handoff"
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "ollama-partial.sqlite3"
            with patch(
                "agent._ollama_exchange",
                side_effect=[
                    _mock_ollama_response(plan_section),
                    AgentError("Could not connect to Ollama: connection refused."),
                ],
            ):
                with self.assertRaisesRegex(
                    AgentError, "validated POC plan is preserved"
                ):
                    create_plan(
                        company="Northstar Bank",
                        industry="Financial services",
                        brief="UK KYC onboarding identity document review.",
                        database_path=database_path,
                        data_dir=DATA_DIR,
                        event_timestamp=EVENT_TIMESTAMP,
                        owner=context["owner"],
                        opportunity_id=context["opportunity_id"],
                        region=context["region"],
                        mode="ollama",
                    )

            self.assertEqual([], get_recent_plans(database_path))
            events = get_workflow_events(
                database_path,
                opportunity_id=context["opportunity_id"],
            )
            partial_events = [
                event for event in events
                if event["event_type"] == "plan_generation_partial"
            ]
            self.assertEqual(1, len(partial_events))
            saved_partial = partial_events[0]["payload"]["plan"]
            self.assertEqual("Northstar Bank POC", saved_partial["title"])
            self.assertNotIn("delivery_handoff", saved_partial)
            self.assertEqual(
                "partial",
                saved_partial["generation_metadata"]["generation_status"],
            )

    def test_seeded_opportunity_events_are_repeatable_and_complete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "seeded.sqlite3"
            seed_path = DATA_DIR / "briefs.json"
            initialize_database(database_path, seed_path)
            initialize_database(database_path, seed_path)

            events = get_opportunity_events(database_path)
            self.assertGreaterEqual(len(events), 3)
            self.assertEqual(
                len(events),
                len({event["opportunity_id"] for event in events}),
            )
            for event in events:
                for field in (
                    "opportunity_id",
                    "event_timestamp",
                    "owner",
                    "account",
                    "region",
                    "customer_brief",
                ):
                    self.assertTrue(event[field], field)

    def test_plan_intake_uses_seeded_event_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "seeded-plan.sqlite3"
            event = {
                "opportunity_id": "opp-1001",
                "timestamp": "2026-10-26T08:30:00Z",
                "owner": "Avery Morgan",
                "account": "Northstar Community Bank",
                "region": "UK",
                "customer_brief": (
                    "We need to reduce delays in retail customer onboarding. "
                    "Analysts currently review identity documents and re-enter "
                    "customer details across separate systems. We want to test "
                    "document extraction and verification with a human review "
                    "step, while keeping an audit trail and measuring onboarding "
                    "turnaround time."
                ),
            }
            _, plan_id = create_plan(
                company=event["account"],
                industry="Financial services",
                brief=event["customer_brief"],
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=event["timestamp"],
                owner=event["owner"],
                opportunity_id=event["opportunity_id"],
                region=event["region"],
                mode="demo",
            )
            plan = get_plan(database_path, plan_id)
            self.assertEqual(event["opportunity_id"], plan["opportunity_id"])
            self.assertEqual(event["timestamp"], plan["event_timestamp"])
            self.assertEqual(event["owner"], plan["owner"])
            self.assertEqual(event["account"], plan["company"])
            self.assertEqual(event["region"], plan["region"])

            with self.assertRaisesRegex(ValueError, "must match the selected source event"):
                create_plan(
                    company=event["account"],
                    industry="Financial services",
                    brief=event["customer_brief"],
                    database_path=database_path,
                    data_dir=DATA_DIR,
                    event_timestamp="2026-10-27T08:30:00Z",
                    owner=event["owner"],
                    opportunity_id=event["opportunity_id"],
                    region=event["region"],
                    mode="demo",
                )

    def test_dashboard_queue_and_template_reuse_metrics(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "dashboard-data.sqlite3"
            event = next(
                item for item in json.loads(
                    (DATA_DIR / "briefs.json").read_text(encoding="utf-8")
                )
                if item["opportunity_id"] == "opp-1001"
            )
            plan, plan_id = create_plan(
                company=event["account"],
                industry="Financial services",
                brief=event["customer_brief"],
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=event["timestamp"],
                owner=event["owner"],
                opportunity_id=event["opportunity_id"],
                region=event["region"],
                mode="demo",
            )

            queue = get_opportunity_queue(database_path)
            queued = next(
                item for item in queue
                if item["opportunity_id"] == event["opportunity_id"]
            )
            self.assertEqual(plan_id, queued["latest_plan_id"])
            self.assertEqual("open", queued["latest_plan_status"])
            self.assertEqual(event["account"], queued["account"])

            metrics = get_template_reuse_metrics(database_path)
            matched_template_ids = {
                item["id"]
                for item in plan["evidence"]
                if item.get("type") == "template"
            }
            self.assertTrue(matched_template_ids)
            self.assertEqual(
                matched_template_ids,
                {item["template_id"] for item in metrics},
            )
            self.assertTrue(all(item["reuse_count"] == 1 for item in metrics))
            self.assertTrue(all(item["accepted_count"] == 0 for item in metrics))
            self.assertTrue(all(item["retained_count"] == 0 for item in metrics))

            review_plan(database_path, plan_id, "accept")
            accepted_metrics = get_template_reuse_metrics(database_path)
            self.assertTrue(all(item["accepted_count"] == 1 for item in accepted_metrics))
            self.assertTrue(all(item["retained_count"] == 1 for item in accepted_metrics))
            self.assertTrue(
                all(
                    get_template_ranking_feedback(database_path)[template_id] == 0.05
                    for template_id in matched_template_ids
                )
            )

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
                mode="demo",
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
                datetime.fromisoformat(EVENT_TIMESTAMP.replace("Z", "+00:00")),
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
                mode="demo",
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
            edited_metrics = get_template_reuse_metrics(database_path)
            self.assertTrue(
                all(metric["accepted_count"] == 0 for metric in edited_metrics)
            )
            self.assertFalse(get_template_ranking_feedback(database_path))

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
            self.assertEqual("opp-test", decisions[0]["opportunity_id"])
            self.assertTrue(json.loads(decisions[1]["edited_plan_json"])["title"])
            rejected_template_ids = json.loads(decisions[0]["template_ids_json"])
            self.assertTrue(rejected_template_ids)
            self.assertEqual([], json.loads(decisions[0]["retained_template_ids_json"]))

            initialize_database(database_path)
            reconnected_plan = get_plan(database_path, plan_id)
            reconnected_decisions = get_review_decisions(database_path, plan_id)
            self.assertEqual("rejected", reconnected_plan["status"])
            self.assertEqual(
                ["reject", "edit", "accept"],
                [decision["decision"] for decision in reconnected_decisions],
            )
            self.assertTrue(
                all(decision["timestamp"] for decision in reconnected_decisions)
            )
            review_events = get_workflow_events(database_path, plan_id=plan_id)
            self.assertEqual(
                3,
                sum(
                    event["event_type"] == "review_decision_recorded"
                    for event in review_events
                ),
            )
            self.assertTrue(
                all(event["payload"]["reviewer_role"] == "US Solution Architect"
                    for event in review_events
                    if event["event_type"] == "review_decision_recorded")
            )
            ranking_feedback = get_template_ranking_feedback(database_path)
            self.assertEqual(
                {template_id: -0.05 for template_id in rejected_template_ids},
                ranking_feedback,
            )
            metrics_before_refresh = get_template_reuse_metrics(database_path)
            self.assertEqual(metrics_before_refresh, get_template_reuse_metrics(database_path))

    def test_feedback_adjusts_template_ranking_without_changing_match_reason(self):
        brief = "UK KYC onboarding identity documents verification"
        baseline = retrieve_context(brief, DATA_DIR, limit=20)
        template = next(item for item in baseline if item["type"] == "template")
        positive = retrieve_context(
            brief,
            DATA_DIR,
            limit=20,
            ranking_feedback={template["id"]: 0.1},
        )
        adjusted = next(item for item in positive if item["id"] == template["id"])
        self.assertEqual(template["reason"], adjusted["reason"])
        self.assertAlmostEqual(
            min(1.0, template["base_score"] + 0.1),
            adjusted["score"],
        )
        self.assertEqual(0.1, adjusted["ranking_adjustment"])

    def test_draft_usage_is_separate_from_templates_retained_after_edit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "template-retention.sqlite3"
            plan, plan_id = create_plan(
                company="Acme",
                industry="Financial services",
                brief="UK CRM customer data quality and pipeline reporting.",
                database_path=database_path,
                data_dir=DATA_DIR,
                event_timestamp=EVENT_TIMESTAMP,
                owner="Avery Morgan",
                opportunity_id="opp-retention",
                region="UK",
                mode="demo",
            )
            drafted_ids = [
                item["id"]
                for item in plan["evidence"]
                if item.get("type") == "template"
            ]
            self.assertTrue(drafted_ids)
            edited_plan = dict(plan)
            edited_plan["evidence"] = [
                item
                for item in plan["evidence"]
                if item.get("type") != "template"
                or item.get("id") == drafted_ids[0]
            ]
            review_plan(
                database_path,
                plan_id,
                "edit",
                edited_plan=edited_plan,
                edit_details="Retained only the selected template.",
                retained_template_ids=[drafted_ids[0]],
            )

            metrics = {
                item["template_id"]: item
                for item in get_template_reuse_metrics(database_path)
            }
            for template_id in drafted_ids:
                self.assertEqual(1, metrics[template_id]["draft_use_count"])
                self.assertEqual(
                    1 if template_id == drafted_ids[0] else 0,
                    metrics[template_id]["retained_count"],
                )
            feedback = get_template_ranking_feedback(database_path)
            self.assertEqual({}, feedback)
            self.assertTrue(
                all(
                    feedback.get(template_id, 0) == 0
                    for template_id in drafted_ids[1:]
                )
            )
            self.assertTrue(
                all(item["accepted_count"] == 0 for item in metrics.values())
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
                mode="demo",
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
                first_pickup_at="2026-10-27T15:00:00-04:00",
            ),
            "Pickup exactly at the two-business-day deadline is on time.",
        )
        self.assertEqual(
            "Red",
            get_sla_status(
                event_timestamp,
                now=datetime.fromisoformat("2026-10-28T12:00:00-04:00"),
                first_pickup_at="2026-10-27T15:00:01-04:00",
            ),
            "Pickup one second after the deadline is late.",
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
                mode="demo",
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
                mode="demo",
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
