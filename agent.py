import json
import http.client
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


class AgentError(RuntimeError):
    """Raised when an enabled LLM provider cannot return a usable plan."""

    def __init__(self, message, partial_plan=None):
        super().__init__(message)
        self.partial_plan = partial_plan


logger = logging.getLogger(__name__)
OLLAMA_NUM_PREDICT = 2048
OLLAMA_RETRY_NUM_PREDICT = 3072
OLLAMA_CONNECT_TIMEOUT_SECONDS = 10
OLLAMA_INFERENCE_TIMEOUT_SECONDS = 720
OLLAMA_PLAN_TIMEOUT_SECONDS = 780


def _template_matches(evidence):
    return [
        item
        for item in evidence
        if item.get("type") == "template"
        and isinstance(item.get("content"), dict)
        and item.get("score", 0) > 0
    ][:1]


def _evidence_items(evidence):
    return [
        {
            "id": item.get("id"),
            "type": item.get("type"),
            "title": item["title"],
            "reason": item["reason"],
            "score": round(item["score"], 3),
            "base_score": round(item.get("base_score", item["score"]), 3),
            "ranking_adjustment": round(item.get("ranking_adjustment", 0), 3),
            "content": item.get("content"),
        }
        for item in evidence[:3]
    ]


def _fallback_plan(company, industry, brief, evidence):
    text = brief.lower()
    themes = []
    for keyword, description in (
        ("crm", "CRM data and workflow integration"),
        ("forecast", "forecasting and pipeline visibility"),
        ("automation", "automation of repetitive sales operations"),
        ("lead", "lead routing and qualification"),
        ("pipeline", "pipeline health and deal progression"),
        ("customer", "customer-facing workflow outcomes"),
        ("data", "data quality and system connectivity"),
    ):
        if keyword in text:
            themes.append(description)
    if not themes:
        themes = ["the highest-priority workflow described in the brief"]

    scope = list(dict.fromkeys(themes[:3]))
    templates = _template_matches(evidence)
    template_contents = [item["content"] for item in templates]
    regions = list(
        dict.fromkeys(
            template["region"]
            for template in template_contents
            if template.get("region")
        )
    )
    use_cases = list(
        dict.fromkeys(
            template["use_case"]
            for template in template_contents
            if template.get("use_case")
        )
    )
    descriptions = list(
        dict.fromkeys(
            template["description"]
            for template in template_contents
            if template.get("description")
        )
    )
    capabilities = list(
        dict.fromkeys(
            capability
            for template in template_contents
            for capability in template.get("required_capabilities", [])
            if capability
        )
    )
    components = list(
        dict.fromkeys(
            component
            for template in template_contents
            for component in template.get("reusable_components", [])
            if component
        )
    )

    if templates:
        regional_context = (
            f"Apply the {', '.join(regions)} template context; confirm applicable "
            "customer policies and data-handling requirements during discovery. "
            if regions
            else ""
        )
        proposed_solution = (
            f"Adapt the {', '.join(use_cases) or 'matched solution'} pattern for "
            f"{company}. {regional_context}{' '.join(descriptions)}"
        )
        template_scope = [
            f"{template['title']}: {template['use_case']}"
            for template in template_contents
            if template.get("title") and template.get("use_case")
        ]
        recommended_scope = list(dict.fromkeys(template_scope + components[:5]))
        required_data_integrations = capabilities
        implementation_tasks = [
            f"Confirm approved data access and readiness for: {capability}."
            for capability in capabilities[:3]
        ]
        configuration_tasks = [
            f"Configure and validate the reusable component: {component}."
            for component in components[:4]
        ]
        phase_two_deliverable = (
            "Configured template components: " + "; ".join(components[:4])
            if components
            else "Configured, testable solution workflow based on matched templates"
        )
        template_risks = [
            f"Template dependency — confirm availability of {capability.lower()} "
            "before the pilot."
            for capability in capabilities[:2]
        ]
        template_assumptions = [
            f"Customer will confirm the applicability of the {region} template "
            "context and its policy requirements."
            for region in regions
        ]
        delivery_handoff = [
            f"Confirm owner and access for: {capability}."
            for capability in capabilities[:3]
        ] + [
            f"Review operation and support of: {component}."
            for component in components[:3]
        ]
    else:
        proposed_solution = (
            "No suitable reusable template was identified. Define a generic, "
            "narrow proof of concept from the customer brief, validate feasibility "
            "during discovery, and keep human review for consequential decisions."
        )
        recommended_scope = scope + [
            "Use a representative, permissioned data sample and document required integrations.",
            "Keep the first release to one primary user workflow and a small pilot group.",
        ]
        required_data_integrations = [
            "Confirm the source systems, required fields, and approved sample data.",
            "Identify integration owners, access method, and security constraints.",
        ]
        implementation_tasks = [
            "Confirm the source data, access method, and stakeholder-owned workflow.",
            "Define the smallest testable workflow from the stated customer objective.",
        ]
        configuration_tasks = [
            "Configure the scoped workflow and review outputs with representative users."
        ]
        phase_two_deliverable = "Working workflow tested with sample scenarios"
        template_risks = []
        template_assumptions = [
            "No suitable reusable template was identified; solution design and feasibility "
            "must be validated during discovery."
        ]
        delivery_handoff = [
            "Confirm system and data owners, access approvals, and support contacts.",
            "Document workflow limitations, unresolved risks, and the pilot decision.",
        ]

    evidence_items = _evidence_items(evidence)
    template_match_explanations = [
        {
            "template_id": item.get("id"),
            "title": item.get("title"),
            "reason": item.get("reason"),
            "relevance_score": round(item.get("score", 0), 3),
            "cited_fields": {
                key: item["content"].get(key)
                for key in (
                    "region",
                    "use_case",
                    "description",
                    "required_capabilities",
                    "reusable_components",
                )
                if item["content"].get(key)
            },
        }
        for item in templates
    ]
    template_reuse = [
        {
            "template_id": item["id"],
            "title": item["title"],
            "source_region": item["content"].get("region", "Unspecified"),
            "relevance_explanation": item["reason"],
            "reusable_components": item["content"].get("reusable_components", []),
            "adaptations": [
                f"Validate the {item['content'].get('region', 'template')} process and data rules with the customer."
            ],
            "limitations": [
                "Template content is internal guidance and does not confirm customer policy, data access, or integration readiness."
            ],
        }
        for item in templates
    ]
    deliverables = [
        "Signed-off POC brief and success scorecard",
        phase_two_deliverable,
        "POC readout and go / iterate / stop recommendation",
    ]
    exclusions = [
        "Production deployment and unattended consequential decisions are out of scope.",
        "Unconfirmed data sources, integrations, and policy interpretations are excluded until discovery.",
    ]
    dependencies = list(dict.fromkeys(required_data_integrations))
    return {
        "title": f"{company} — {industry} POC plan",
        "customer_context": {
            "account": company,
            "opportunity_id": None,
            "region": regions[0] if regions else None,
            "owner": None,
        },
        "facts_from_brief": [brief],
        "business_problem": brief,
        "objectives": scope,
        "executive_summary": (
            f"Validate a focused, measurable solution for {company} by testing "
            f"{', '.join(scope)} against the outcomes in the customer brief. "
            "Start with a bounded data sample and agree on success criteria "
            "before building."
        ),
        "proposed_solution": proposed_solution,
        "architecture_outline": (
            "Customer-approved data sources feed a bounded prototype workflow; "
            "configured components produce reviewable outputs, with human review "
            "and audit capture before any downstream action."
        ),
        "recommended_scope": recommended_scope,
        "exclusions": exclusions,
        "deliverables": deliverables,
        "required_data_integrations": required_data_integrations,
        "data_security_considerations": [
            "Confirm data classification, access controls, retention, privacy, and approved processing location with customer owners."
        ],
        "human_review_requirements": [
            "Route low-confidence and exception cases to an authorized human reviewer.",
            "Do not automate lending, coverage, identity, fraud disposition, or other consequential customer decisions."
        ],
        "phases": [
            {
                "name": "Align and qualify",
                "duration": "Days 1–2",
                "tasks": [
                    "Confirm the business owner, users, current workflow, and baseline.",
                    "Agree on data access, security constraints, and measurable acceptance criteria.",
                ] + implementation_tasks,
                "deliverable": "Signed-off POC brief and success scorecard",
                "acceptance_criteria": [
                    "Customer owners confirm scope, data access, baseline, and success measures."
                ],
            },
            {
                "name": "Configure and validate",
                "duration": "Days 3–7",
                "tasks": [
                    "Connect or load an approved sample and configure the scoped workflow.",
                ] + configuration_tasks + [
                    "Review outputs with representative users and capture issues.",
                ],
                "deliverable": phase_two_deliverable,
                "acceptance_criteria": [
                    "Representative scenarios are reviewable and traceable, with exceptions routed to the agreed human owner."
                ],
            },
            {
                "name": "Pilot and decide",
                "duration": "Days 8–10",
                "tasks": [
                    "Run the pilot against agreed scenarios and compare results to baseline.",
                    "Review adoption, outcome metrics, open risks, and scale requirements.",
                ],
                "deliverable": "POC readout and go / iterate / stop recommendation",
                "acceptance_criteria": [
                    "Results are compared with the agreed baseline and signed off by the named business owner."
                ],
            },
        ],
        "success_metrics": [
            "At least 80% of agreed pilot scenarios complete successfully.",
            "Measure change against an agreed baseline for the primary business outcome.",
            "Pilot users rate workflow usefulness at least 4/5.",
            "No unresolved critical data, security, or access-control issues.",
        ],
        "acceptance_criteria": [
            "Agree baseline and numeric thresholds with the customer before the pilot.",
            "Complete at least 80% of agreed scenarios without unresolved critical issues.",
            "Obtain named business-owner sign-off on the POC readout.",
        ],
        "evaluation_strategy": [
            "Create a representative, customer-approved evaluation sample and label expected outputs with authorized reviewers.",
            "Measure task completion, field-level accuracy where relevant, exception rate, review time, and critical-error rate against agreed thresholds."
        ],
        "risks": [
            "Incomplete or inconsistent source data — validate a representative sample before configuration.",
            "Unclear success criteria — establish baseline, owner, and thresholds during kickoff.",
            "Scope expansion — use explicit in-scope workflows and a change log.",
            "Integration or security delays — identify system owners and approvals in the first phase.",
        ] + template_risks,
        "assumptions": [
            "The customer can provide an approved sample and a stakeholder for timely decisions.",
            "The 10-business-day plan is indicative and may shift based on access and integration readiness.",
            "Final thresholds and technical feasibility will be confirmed during discovery.",
        ] + template_assumptions,
        "dependencies": dependencies,
        "open_questions": [
            "Which exact source systems, documents, and data fields are approved for the pilot?",
            "Who is accountable for reviewing exceptions and approving customer-facing or consequential outputs?",
            "What baseline and numeric thresholds will define success?"
        ],
        "timeline_estimate": {
            "duration": "10 business days",
            "qualification": "Indicative estimate; confirm after data, access, and integration discovery."
        },
        "exit_criteria": [
            "Complete the agreed evaluation set and document results against approved thresholds.",
            "Resolve or explicitly accept all critical risks before a scale decision."
        ],
        "template_reuse": template_reuse,
        "next_steps": [
            "Schedule a 45-minute discovery and success-criteria workshop.",
            "Request a sample dataset, system diagram, and security requirements.",
            "Name customer and delivery owners and confirm pilot participants.",
        ],
        "delivery_handoff": {
            "customer_context": f"{company} · {industry}. {brief}",
            "scope": recommended_scope,
            "proposed_architecture": (
                "Approved sample data -> scoped prototype workflow -> human review "
                "and audit trail -> measured pilot readout."
            ),
            "dependencies": dependencies,
            "ownership": [
                f"Opportunity owner: confirm customer stakeholders and source-data access ({company}).",
                "US Solution Architect: approve customer-facing scope and acceptance criteria.",
                "Delivery lead: confirm implementation owner and operational handoff.",
            ],
            "deliverables": deliverables,
            "risks": template_risks + [
                "Data access, quality, or security approvals may delay the pilot."
            ],
            "open_questions": [
                "Which systems and data fields are approved for the pilot?",
                "Who owns operational review, exception handling, and final decisions?",
                "What baseline and thresholds constitute success?",
            ],
            "checklist": delivery_handoff,
        },
        "evidence": evidence_items,
        "template_match_explanations": template_match_explanations,
        "generation_mode": "demo_fixture",
    }


_PLAN_LIST_FIELDS = (
    "facts_from_brief",
    "objectives",
    "recommended_scope",
    "exclusions",
    "deliverables",
    "phases",
    "required_data_integrations",
    "data_security_considerations",
    "human_review_requirements",
    "success_metrics",
    "evaluation_strategy",
    "acceptance_criteria",
    "risks",
    "assumptions",
    "dependencies",
    "open_questions",
    "exit_criteria",
    "next_steps",
    "template_reuse",
)
_HANDOFF_LIST_FIELDS = (
    "in_scope",
    "out_of_scope",
    "functional_requirements",
    "expected_workflow",
    "proposed_components",
    "integration_boundaries",
    "data_inputs",
    "data_outputs",
    "dependencies",
    "security_privacy_access_audit",
    "milestones",
    "engineering_tasks",
    "deliverables",
    "acceptance_criteria",
    "evaluation_metrics",
    "risks_blockers",
    "assumptions",
    "customer_questions",
    "solution_architect_confirmations",
    "delivery_lead_confirmations",
)


def _ollama_prompt(company, industry, brief, evidence, opportunity_context):
    templates = _template_matches(evidence)
    return {
        "customer_context": {
            "account": company,
            "industry": industry,
            **{
                field: (opportunity_context or {}).get(field)
                for field in ("opportunity_id", "region", "owner")
            },
        },
        "customer_brief": brief,
        "retrieved_templates": [
            {
                "id": item["id"],
                "title": item["title"],
                "source_region": item["content"].get("region"),
                "template_fields": {
                    "use_case": item["content"].get("use_case"),
                    "description": str(item["content"].get("description", ""))[:280],
                    "required_capabilities": item["content"].get(
                        "required_capabilities", []
                    )[:3],
                    "reusable_components": item["content"].get(
                        "reusable_components", []
                    )[:3],
                },
            }
            for item in templates
        ],
        "required_json_shape": {
            "title": "string",
            "customer_context": {
                "account": "exact account name",
                "industry": "exact selected industry",
                "opportunity_id": "selected ID or null",
                "region": "selected region or null",
                "owner": "selected owner or null",
            },
            "facts_from_brief": [{"fact": "string", "evidence_quote": "verbatim brief excerpt"}],
            "business_problem": "string",
            "objectives": ["string"],
            "executive_summary": "string",
            "proposed_solution": "string",
            "architecture_outline": "string",
            "recommended_scope": ["string"],
            "exclusions": ["string"],
            "deliverables": ["string"],
            "phases": [
                {
                    "name": "string",
                    "duration": "estimate",
                    "tasks": ["string"],
                    "deliverable": "string",
                    "acceptance_criteria": ["string"],
                }
            ],
            "required_data_integrations": ["string"],
            "data_security_considerations": ["string"],
            "human_review_requirements": ["string"],
            "success_metrics": ["measurable criteria or measurement method"],
            "evaluation_strategy": ["string"],
            "acceptance_criteria": ["string"],
            "risks": ["string"],
            "assumptions": ["string"],
            "dependencies": ["string"],
            "open_questions": ["string"],
            "timeline_estimate": {"duration": "estimate", "qualification": "string"},
            "exit_criteria": ["string"],
            "next_steps": ["string"],
            "template_reuse": [
                {
                    "template_id": "retrieved ID only",
                    "title": "exact retrieved title",
                    "source_region": "exact region",
                    "relevance_explanation": "specific match rationale",
                    "reusable_components": ["exact component names"],
                    "adaptations": ["required customer adaptation"],
                    "limitations": ["mismatch or uncertainty"],
                }
            ],
            "delivery_handoff": {
                "business_problem": "string",
                "desired_outcome": "string",
                **{field: ["string"] for field in _HANDOFF_LIST_FIELDS},
            },
        },
        "instructions": (
            "Return compact, customer-specific JSON. Keep each string to 10 words "
            "where possible; use one item per required list unless more are essential. "
            "Use at most two concise phases, with one or two tasks each. Avoid duplicate "
            "explanations and repeated template descriptions. Cite one exact brief quote "
            "per fact; use only supplied template IDs and component names. Do not invent "
            "integrations, approvals, metrics, or commitments. Label estimates and "
            "assumptions; retain human approval for consequential decisions. If no "
            "template fits, make template_reuse an empty array and say so explicitly. "
            "Return every required key and valid JSON only."
        ),
    }


def _ground_facts_from_brief(plan, brief):
    """Ensure brief facts cite text that is actually present in the source brief.

    Small local models sometimes paraphrase or slightly alter evidence quotes. Rather
    than spend another inference call repairing a deterministic provenance issue, use
    a short exact excerpt from the supplied brief as the grounded fact when citations
    are missing or invalid. This does not add information beyond the brief.
    """
    if not isinstance(plan, dict) or not isinstance(brief, str) or not brief.strip():
        return plan

    facts = plan.get("facts_from_brief")
    if isinstance(facts, list) and facts:
        all_grounded = all(
            isinstance(item, dict)
            and isinstance(item.get("fact"), str)
            and item["fact"].strip()
            and isinstance(item.get("evidence_quote"), str)
            and item["evidence_quote"].strip()
            and item["evidence_quote"].casefold() in brief.casefold()
            for item in facts
        )
        if all_grounded:
            return plan

    # Keep the quote as a literal substring of the brief. Short excerpts reduce
    # repeated prompt/output tokens while remaining directly auditable.
    source = brief.strip()
    quote = source[:240]
    if len(source) > len(quote):
        boundary = quote.rfind(" ")
        if boundary >= 80:
            quote = quote[:boundary]
    plan["facts_from_brief"] = [
        {
            "fact": f"Customer brief states: {quote}",
            "evidence_quote": quote,
        }
    ]
    logger.warning(
        "Replaced ungrounded model evidence with an exact brief excerpt "
        "(brief_chars=%d, excerpt_chars=%d)",
        len(brief),
        len(quote),
    )
    return plan


def _ensure_business_problem_account_reference(plan, company):
    """Make the business-problem statement explicitly identify the selected account.

    This is deterministic context grounding, not model-generated evidence: the
    selected account comes from the opportunity event. Preserve the model's
    problem description and add the account name only when it is absent.
    """
    if not isinstance(plan, dict) or not isinstance(company, str) or not company.strip():
        return plan
    problem = plan.get("business_problem")
    if isinstance(problem, str) and problem.strip():
        if company.casefold() not in problem.casefold():
            plan["business_problem"] = f"{company}: {problem.strip()}"
    return plan


def _validate_ollama_plan(
    plan,
    company,
    industry,
    brief,
    evidence,
    opportunity_context,
    validate_handoff=True,
):
    if not isinstance(plan, dict):
        raise ValueError("the response must be a JSON object")
    for field in (
        "title",
        "business_problem",
        "executive_summary",
        "proposed_solution",
        "architecture_outline",
    ):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"{field} must be non-empty text")
    for field in _PLAN_LIST_FIELDS:
        value = plan.get(field)
        if not isinstance(value, list):
            raise ValueError(f"{field} must be an array")
        if field != "template_reuse" and not value:
            raise ValueError(f"{field} must not be empty")
        if field not in {"facts_from_brief", "phases", "template_reuse"} and any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{field} must contain non-empty strings")
    for index, fact in enumerate(plan["facts_from_brief"]):
        if (
            not isinstance(fact, dict)
            or not isinstance(fact.get("fact"), str)
            or not fact["fact"].strip()
            or not isinstance(fact.get("evidence_quote"), str)
            or not fact["evidence_quote"].strip()
            or fact["evidence_quote"].casefold() not in brief.casefold()
        ):
            raise ValueError(
                f"facts_from_brief[{index}] must cite a verbatim brief excerpt"
            )
    context = plan.get("customer_context")
    if not isinstance(context, dict):
        raise ValueError("customer_context must be a JSON object")
    if context.get("account") != company:
        raise ValueError("customer_context.account must match the selected account")
    if context.get("industry") != industry:
        raise ValueError("customer_context.industry must match the selected industry")
    for field in ("opportunity_id", "region", "owner"):
        expected = (opportunity_context or {}).get(field)
        if context.get(field) != expected:
            raise ValueError(f"customer_context.{field} must match the selected opportunity")

    measurable = " ".join(str(value).lower() for value in plan["success_metrics"])
    if not any(
        word in measurable
        for word in ("measure", "rate", "%", "accuracy", "time", "threshold",
                     "baseline", "count", "precision", "recall", "target")
    ):
        raise ValueError("success_metrics must include a measurable criterion")
    for index, phase in enumerate(plan["phases"]):
        if not isinstance(phase, dict):
            raise ValueError(f"phases[{index}] must be an object")
        for field in ("name", "duration", "deliverable"):
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                raise ValueError(f"phases[{index}].{field} must be non-empty text")
        for field in ("tasks", "acceptance_criteria"):
            values = phase.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise ValueError(
                    f"phases[{index}].{field} must be a non-empty array of strings"
                )

    timeline = plan.get("timeline_estimate")
    if not isinstance(timeline, dict) or not all(
        isinstance(timeline.get(field), str) and timeline[field].strip()
        for field in ("duration", "qualification")
    ):
        raise ValueError("timeline_estimate must qualify an estimated duration")
    if validate_handoff:
        handoff = plan.get("delivery_handoff")
        if not isinstance(handoff, dict):
            raise ValueError("delivery_handoff must be a JSON object")
        for field in ("business_problem", "desired_outcome"):
            if not isinstance(handoff.get(field), str) or not handoff[field].strip():
                raise ValueError(f"delivery_handoff.{field} must be non-empty text")
        for field in _HANDOFF_LIST_FIELDS:
            if (
                not isinstance(handoff.get(field), list)
                or not handoff[field]
                or any(not isinstance(item, str) or not item.strip() for item in handoff[field])
            ):
                raise ValueError(f"delivery_handoff.{field} must be a non-empty array")

    available = {item["id"]: item for item in _template_matches(evidence)}
    seen = set()
    for index, reuse in enumerate(plan["template_reuse"]):
        if not isinstance(reuse, dict):
            raise ValueError(f"template_reuse[{index}] must be an object")
        template_id = reuse.get("template_id")
        if template_id not in available:
            raise ValueError(f"template_reuse references non-retrieved ID: {template_id}")
        if template_id in seen:
            raise ValueError(f"duplicate template_reuse ID: {template_id}")
        seen.add(template_id)
        template = available[template_id]["content"]
        if reuse.get("title") != template.get("title"):
            raise ValueError(f"template_reuse title does not match {template_id}")
        if reuse.get("source_region") != template.get("region"):
            raise ValueError(f"template_reuse region does not match {template_id}")
        if not isinstance(reuse.get("relevance_explanation"), str) or not reuse["relevance_explanation"].strip():
            raise ValueError(f"template_reuse explanation missing for {template_id}")
        components = reuse.get("reusable_components")
        if not isinstance(components, list) or not components or not set(components).issubset(
            set(template.get("reusable_components", []))
        ):
            raise ValueError(f"template_reuse contains unknown components for {template_id}")
        for field in ("adaptations", "limitations"):
            values = reuse.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise ValueError(f"template_reuse.{field} required for {template_id}")
    if not available and not any(
        "no suitable reusable template" in str(item).lower()
        for item in (plan["proposed_solution"], *plan["assumptions"])
    ):
        raise ValueError("explicitly state that no suitable reusable template was identified")
    if available and not plan["template_reuse"] and not any(
        "no suitable reusable template" in str(item).lower()
        for item in (plan["proposed_solution"], *plan["assumptions"])
    ):
        raise ValueError(
            "an empty template_reuse list must explain why no retrieved template fits"
        )

    if not any(word in plan["business_problem"].lower() for word in company.lower().split() if len(word) > 3):
        raise ValueError("business_problem must refer to the selected account")
    plan["evidence"] = _evidence_items(evidence)
    plan["template_match_explanations"] = [
        {
            "template_id": item["id"],
            "title": item["title"],
            "reason": item["reason"],
            "relevance_score": round(item["score"], 3),
            "cited_fields": {
                key: item["content"][key]
                for key in ("region", "use_case", "required_capabilities", "reusable_components")
                if item["content"].get(key)
            },
        }
        for item in _template_matches(evidence)
    ]
    return plan


def _ollama_exchange(
    endpoint,
    request_body,
    inference_timeout_seconds=OLLAMA_INFERENCE_TIMEOUT_SECONDS,
):
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
        raise AgentError("OLLAMA_BASE_URL must be an HTTP(S) URL with a hostname.")
    connection_type = (
        http.client.HTTPSConnection
        if parsed_endpoint.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(
        parsed_endpoint.hostname,
        parsed_endpoint.port,
        timeout=OLLAMA_CONNECT_TIMEOUT_SECONDS,
    )
    request_path = parsed_endpoint.path or "/"
    if not request_path.endswith("/api/chat"):
        request_path = request_path.rstrip("/") + "/api/chat"
    if parsed_endpoint.query:
        request_path += f"?{parsed_endpoint.query}"

    try:
        try:
            connection.connect()
        except (socket.timeout, TimeoutError) as error:
            raise AgentError(
                f"Timed out connecting to Ollama after "
                f"{OLLAMA_CONNECT_TIMEOUT_SECONDS} seconds."
            ) from error
        except OSError as error:
            raise AgentError(f"Could not connect to Ollama: {error}.") from error

        deadline = time.perf_counter() + inference_timeout_seconds

        def set_remaining_timeout():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("overall inference deadline exceeded")
            connection.sock.settimeout(remaining)

        try:
            set_remaining_timeout()
            connection.request(
                "POST",
                request_path,
                body=request_body,
                headers={"Content-Type": "application/json"},
            )
            set_remaining_timeout()
            response = connection.getresponse()
            set_remaining_timeout()
            response_body = response.read()
        except (socket.timeout, TimeoutError) as error:
            raise AgentError(
                "Ollama inference timed out after "
                f"{inference_timeout_seconds:.0f} seconds; "
                "the TCP connection was established but the response did not complete."
            ) from error
        except OSError as error:
            raise AgentError(
                f"Ollama connection failed while waiting for the response: {error}."
            ) from error

        if response.status >= 400:
            detail = response_body.decode("utf-8", errors="replace")[:500]
            raise AgentError(f"Ollama returned HTTP {response.status}: {detail}")
        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AgentError(f"Ollama returned malformed API JSON: {error}") from error
    finally:
        connection.close()

    if not isinstance(result, dict):
        raise AgentError("Ollama API response must be a JSON object.")
    return result


def _ollama_token_budgets():
    try:
        initial = int(os.getenv("OLLAMA_NUM_PREDICT", str(OLLAMA_NUM_PREDICT)))
        retry_default = max(OLLAMA_RETRY_NUM_PREDICT, initial + 1)
        retry = int(os.getenv("OLLAMA_RETRY_NUM_PREDICT", str(retry_default)))
    except ValueError as error:
        raise AgentError("Ollama token limits must be positive integers.") from error
    if initial <= 0 or retry <= initial:
        raise AgentError(
            "OLLAMA_NUM_PREDICT must be positive and OLLAMA_RETRY_NUM_PREDICT "
            "must be greater than OLLAMA_NUM_PREDICT."
        )
    return initial, retry


def _ollama_chat(
    base_url,
    model,
    messages,
    call_type,
    inference_timeout_seconds=None,
    num_predict=OLLAMA_NUM_PREDICT,
):
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/api/chat"):
        endpoint += "/api/chat"
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0.2, "num_predict": num_predict},
            "messages": messages,
        }
    ).encode("utf-8")
    started_at = time.perf_counter()
    request_timeout = min(
        OLLAMA_INFERENCE_TIMEOUT_SECONDS,
        inference_timeout_seconds or OLLAMA_INFERENCE_TIMEOUT_SECONDS,
    )
    logger.info(
        "Ollama %s request started; prompt_bytes=%d output_token_limit=%d "
        "connect_timeout=%ds inference_timeout=%ds",
        call_type,
        len(body),
        num_predict,
        OLLAMA_CONNECT_TIMEOUT_SECONDS,
        request_timeout,
    )
    try:
        result = _ollama_exchange(endpoint, body, request_timeout)
    except AgentError as error:
        logger.warning(
            "Ollama %s call failed after %.2fs (%s)",
            call_type,
            time.perf_counter() - started_at,
            type(error).__name__,
        )
        raise
    elapsed = time.perf_counter() - started_at
    response_metadata = {
        "done": result.get("done"),
        "done_reason": result.get("done_reason"),
        "prompt_eval_count": result.get("prompt_eval_count"),
        "eval_count": result.get("eval_count"),
        "total_duration": result.get("total_duration"),
        "elapsed_seconds": round(elapsed, 3),
        "request_bytes": len(body),
    }
    logger.info(
        "Ollama %s call completed in %.2fs; prompt_eval_count=%r eval_count=%r "
        "total_duration=%r done=%r done_reason=%r",
        call_type,
        elapsed,
        response_metadata["prompt_eval_count"],
        response_metadata["eval_count"],
        response_metadata["total_duration"],
        response_metadata["done"],
        response_metadata["done_reason"],
    )
    if result.get("error"):
        raise AgentError(f"Ollama model error: {result['error']}")
    message = result.get("message")
    if not isinstance(message, dict):
        logger.warning("Ollama %s response has no message object", call_type)
        raise AgentError("Ollama API response message must be a JSON object.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        logger.warning("Ollama %s response has no message content", call_type)
        raise AgentError("Ollama response has no message.content.")
    return content, response_metadata


def _escape_control_chars_in_json_strings(text):
    """Escape raw JSON control characters only when they occur inside strings.

    Some local models occasionally emit literal line breaks or tabs inside a
    quoted value even when JSON mode is enabled. JSON permits these characters
    only as escaped sequences. This narrow repair preserves structural
    whitespace and already-escaped characters.
    """
    output = []
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                output.append(character)
                escaped = False
                continue
            if character == "\\":
                output.append(character)
                escaped = True
                continue
            if character == '"':
                output.append(character)
                in_string = False
                continue
            codepoint = ord(character)
            if codepoint < 0x20:
                replacements = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
                output.append(replacements.get(character, f"\\u{codepoint:04x}"))
                continue
            output.append(character)
        else:
            output.append(character)
            if character == '"':
                in_string = True
    return "".join(output)


def _parse_model_json(text, call_type="model"):
    """Parse model JSON, recovering only invalid raw control characters."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        if not error.msg.startswith("Invalid control character"):
            raise
        repaired_text = _escape_control_chars_in_json_strings(text)
        if repaired_text == text:
            raise
        try:
            parsed = json.loads(repaired_text)
        except json.JSONDecodeError:
            raise error
        logger.warning(
            "Ollama %s JSON contained raw control characters inside strings; "
            "escaped them before schema validation (response_chars=%d)",
            call_type,
            len(text),
        )
        return parsed


def _ollama_json_response(
    base_url,
    model,
    messages,
    call_type,
    initial_budget,
    retry_budget,
    generation_started,
):
    def remaining_timeout():
        remaining = OLLAMA_PLAN_TIMEOUT_SECONDS - (
            time.perf_counter() - generation_started
        )
        if remaining <= 0:
            raise AgentError("Overall POC generation deadline reached.")
        return remaining

    output, metadata = _ollama_chat(
        base_url,
        model,
        messages,
        call_type,
        inference_timeout_seconds=remaining_timeout(),
        num_predict=initial_budget,
    )
    attempts = [metadata]
    if metadata["done"] is not True or metadata["done_reason"] == "length":
        logger.warning(
            "Ollama %s response incomplete; done=%r done_reason=%r. "
            "Retrying once with output_token_limit=%d.",
            call_type,
            metadata["done"],
            metadata["done_reason"],
            retry_budget,
        )
        output, retry_metadata = _ollama_chat(
            base_url,
            model,
            messages,
            f"{call_type}_retry",
            inference_timeout_seconds=remaining_timeout(),
            num_predict=retry_budget,
        )
        attempts.append(retry_metadata)
        if retry_metadata["done"] is not True or retry_metadata["done_reason"] == "length":
            diagnostics = (
                f"{call_type} retry incomplete "
                f"(done={retry_metadata['done']!r}, "
                f"done_reason={retry_metadata['done_reason']!r})"
            )
            logger.error("Ollama generation incomplete: %s", diagnostics)
            raise AgentError(f"Ollama generation incomplete; {diagnostics}")
    return output, attempts


def _schema_value_at_path(schema, path):
    current = schema
    segments = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\[\d+\]", path)
    if "".join(segments) != path.replace(".", ""):
        return None
    for segment in path.split("."):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segment)
        if not match or not isinstance(current, dict) or match.group(1) not in current:
            return None
        current = current[match.group(1)]
        if match.group(2) is not None:
            if not isinstance(current, list) or not current:
                return None
            current = current[0]
    return current


def _nested_patch(path, value):
    patch = value
    for segment in reversed(path.split(".")):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segment)
        if not match:
            return {}
        if match.group(2) is not None:
            patch = [patch]
        patch = {match.group(1): patch}
    return patch


def _value_at_path(value, path):
    current = value
    for segment in path.split("."):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segment)
        if not match or not isinstance(current, dict) or match.group(1) not in current:
            raise ValueError(f"repair response omitted {path}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                raise ValueError(f"repair response omitted {path}")
            current = current[index]
    return current


def _set_value_at_path(value, path, replacement):
    segments = path.split(".")
    current = value
    for segment in segments[:-1]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segment)
        if not match or not isinstance(current, dict) or not isinstance(current.get(match.group(1)), (dict, list)):
            raise ValueError(f"cannot apply repair at {path}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                raise ValueError(f"cannot apply repair at {path}")
            current = current[index]
    if not isinstance(current, dict):
        raise ValueError(f"cannot apply repair at {path}")
    final = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", segments[-1])
    if not final:
        raise ValueError(f"cannot apply repair at {path}")
    if final.group(2) is None:
        current[final.group(1)] = replacement
    else:
        index = int(final.group(2))
        target = current[final.group(1)]
        if not isinstance(target, list) or index >= len(target):
            raise ValueError(f"cannot apply repair at {path}")
        target[index] = replacement


def _repair_messages(
    prompt, initial_messages, company, brief, parsed_plan, validation_error
):
    schema = prompt["required_json_shape"]
    path_match = re.match(
        r"^([A-Za-z_]\w*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*)",
        validation_error,
    )
    path = path_match.group(1) if path_match else ""
    field_shape = _schema_value_at_path(schema, path)
    if field_shape is not None:
        contextual_plan = {
            key: parsed_plan.get(key)
            for key in ("customer_context", "phases", "proposed_solution", "next_steps")
            if isinstance(parsed_plan, dict) and key in parsed_plan
        }
        repair_payload = {
            "repair_path": path,
            "required_shape": _nested_patch(path, field_shape),
            "account": company,
            "customer_brief": brief,
            "retrieved_templates": prompt["retrieved_templates"],
            "existing_plan_context": contextual_plan,
            "validation_error": validation_error,
            "instruction": (
                "Return only a JSON object containing the requested path. Do not "
                "repeat or revise other plan sections. Use concise, specific values."
            ),
        }
        return [
            {
                "role": "system",
                "content": "Repair only the requested JSON field. Return JSON only.",
            },
            {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
        ], path

    return initial_messages + [
        {
            "role": "user",
            "content": (
                "The previous response was malformed or incomplete. Regenerate the "
                "complete JSON object matching the schema above; keep it concise and "
                "include every required section."
            ),
        }
    ], None


def _repair_malformed_json_once(
    base_url,
    model,
    malformed_output,
    parse_error,
    required_shape,
    call_type,
    initial_budget,
    retry_budget,
    generation_started,
):
    """Ask Ollama once to reconstruct malformed JSON, then parse it normally.

    This is deliberately separate from schema-validation repair: malformed JSON
    cannot be validated until its syntax is corrected. The raw output is bounded
    to keep the repair prompt and latency under control.
    """
    excerpt = malformed_output[:12000]
    repair_payload = {
        "task": "Rewrite the supplied response as one valid JSON object.",
        "required_json_shape": required_shape,
        "parse_error": str(parse_error),
        "malformed_response": excerpt,
        "rules": [
            "Return JSON only, with no Markdown fences or commentary.",
            "Preserve the original meaning and customer-specific details.",
            "Include all required keys and use the required value types.",
            "Do not invent facts or evidence quotes.",
            "Keep text concise; do not add new sections.",
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Repair malformed JSON for a business application. Return exactly "
                "one syntactically valid JSON object and nothing else."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(repair_payload, ensure_ascii=False),
        },
    ]
    # Syntax repair should be shorter than the original plan generation.
    repair_budget = min(initial_budget, 1024)
    repair_retry_budget = max(repair_budget + 1, min(retry_budget, 1536))
    repaired_output, _ = _ollama_json_response(
        base_url,
        model,
        messages,
        f"{call_type}_json_repair",
        repair_budget,
        repair_retry_budget,
        generation_started,
    )
    return _parse_model_json(repaired_output, f"{call_type}_json_repair")


def _ollama_plan(company, industry, brief, evidence, model, base_url, opportunity_context):
    generation_started = time.perf_counter()
    initial_budget, retry_budget = _ollama_token_budgets()
    plan_prompt = _ollama_prompt(
        company, industry, brief, evidence, opportunity_context
    )
    handoff_shape = plan_prompt["required_json_shape"].pop("delivery_handoff")
    plan_prompt["instructions"] += " Return only POC plan fields; omit the handoff."
    plan_messages = [
        {
            "role": "system",
            "content": "Create a concise, customer-specific POC plan. Return valid JSON only.",
        },
        {"role": "user", "content": json.dumps(plan_prompt, ensure_ascii=False)},
    ]
    plan_output, plan_attempts = _ollama_json_response(
        base_url,
        model,
        plan_messages,
        "poc_plan",
        initial_budget,
        retry_budget,
        generation_started,
    )
    try:
        plan = _parse_model_json(plan_output, "poc_plan")
    except json.JSONDecodeError as parse_error:
        logger.warning(
            "Ollama POC plan returned malformed JSON; attempting one bounded syntax repair: %s",
            parse_error,
        )
        try:
            plan = _repair_malformed_json_once(
                base_url,
                model,
                plan_output,
                parse_error,
                plan_prompt["required_json_shape"],
                "poc_plan",
                initial_budget,
                retry_budget,
                generation_started,
            )
        except (AgentError, json.JSONDecodeError, TypeError, ValueError) as repair_error:
            logger.error("Ollama POC plan JSON repair failed: %s", repair_error)
            raise AgentError(
                "Ollama returned invalid plan JSON after one syntax-repair attempt: "
                f"{repair_error}"
            ) from repair_error
    except (TypeError, ValueError) as error:
        logger.error("Ollama POC plan JSON validation error: %s", error)
        raise AgentError(f"Ollama returned invalid plan JSON: {error}") from error
    if not isinstance(plan, dict):
        raise AgentError("Ollama returned invalid plan JSON: POC plan response must be a JSON object")

    # Evidence provenance is deterministic: normalize malformed model citations
    # before schema validation instead of spending another slow Ollama call on them.
    plan = _ground_facts_from_brief(plan, brief)
    plan = _ensure_business_problem_account_reference(plan, company)
    plan_repair_attempts = []
    try:
        plan = _validate_ollama_plan(
            plan,
            company,
            industry,
            brief,
            evidence,
            opportunity_context,
            validate_handoff=False,
        )
    except (TypeError, ValueError) as error:
        diagnostics = str(error)
        logger.warning("Ollama POC plan validation failed: %s", diagnostics)
        repair_messages, repaired_path = _repair_messages(
            plan_prompt,
            plan_messages,
            company,
            brief,
            plan,
            diagnostics,
        )
        repair_output, plan_repair_attempts = _ollama_json_response(
            base_url,
            model,
            repair_messages,
            "poc_plan_repair",
            initial_budget,
            retry_budget,
            generation_started,
        )
        try:
            repaired = _parse_model_json(repair_output, "poc_plan_repair")
            if not isinstance(repaired, dict):
                raise ValueError("repair response must be a JSON object")
            if repaired_path:
                _set_value_at_path(
                    plan,
                    repaired_path,
                    _value_at_path(repaired, repaired_path),
                )
            else:
                plan = repaired
            plan = _ground_facts_from_brief(plan, brief)
            plan = _ensure_business_problem_account_reference(plan, company)
            plan = _validate_ollama_plan(
                plan,
                company,
                industry,
                brief,
                evidence,
                opportunity_context,
                validate_handoff=False,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as repair_error:
            diagnostics = str(repair_error)
            logger.error("Ollama repaired POC plan validation error: %s", diagnostics)
            raise AgentError(
                "Ollama POC plan failed validation after one repair attempt: "
                f"{diagnostics}"
            ) from repair_error

    handoff_prompt = {
        "customer_context": plan_prompt["customer_context"],
        "retrieved_templates": [],
        "poc_plan_context": {
            key: plan[key]
            for key in (
                "title",
                "business_problem",
                "objectives",
                "proposed_solution",
                "recommended_scope",
                "required_data_integrations",
                "success_metrics",
                "dependencies",
            )
        },
        "required_json_shape": {"delivery_handoff": handoff_shape},
        "instructions": (
            "Return a concise Delivery handoff as valid JSON only. Keep values "
            "customer-specific. Keep each value to 10 words where possible; use one "
            "concise item per required array and 2-3 expected_workflow steps. Do not "
            "repeat plan explanations or template text. Return every required key."
        ),
    }
    handoff_messages = [
        {
            "role": "system",
            "content": "Create a concise Delivery handoff for this POC. Return valid JSON only.",
        },
        {"role": "user", "content": json.dumps(handoff_prompt, ensure_ascii=False)},
    ]
    try:
        handoff_output, handoff_attempts = _ollama_json_response(
            base_url,
            model,
            handoff_messages,
            "delivery_handoff",
            initial_budget,
            retry_budget,
            generation_started,
        )
        try:
            handoff = _parse_model_json(handoff_output, "delivery_handoff")
        except json.JSONDecodeError as parse_error:
            logger.warning(
                "Ollama Delivery handoff returned malformed JSON; attempting one bounded syntax repair: %s",
                parse_error,
            )
            handoff = _repair_malformed_json_once(
                base_url,
                model,
                handoff_output,
                parse_error,
                handoff_prompt["required_json_shape"],
                "delivery_handoff",
                initial_budget,
                retry_budget,
                generation_started,
            )
        if not isinstance(handoff, dict):
            raise ValueError("Delivery handoff response must be a JSON object")
        plan.update(handoff)
        handoff_repair_attempts = []
        try:
            plan = _validate_ollama_plan(
                plan, company, industry, brief, evidence, opportunity_context
            )
        except (TypeError, ValueError) as error:
            diagnostics = str(error)
            logger.warning("Ollama Delivery handoff validation failed: %s", diagnostics)
            repair_messages, repaired_path = _repair_messages(
                handoff_prompt,
                handoff_messages,
                company,
                brief,
                plan,
                diagnostics,
            )
            repair_output, handoff_repair_attempts = _ollama_json_response(
                base_url,
                model,
                repair_messages,
                "delivery_handoff_repair",
                initial_budget,
                retry_budget,
                generation_started,
            )
            repaired = _parse_model_json(repair_output, "delivery_handoff_repair")
            if not isinstance(repaired, dict):
                raise ValueError("handoff repair response must be a JSON object")
            if repaired_path:
                _set_value_at_path(
                    plan,
                    repaired_path,
                    _value_at_path(repaired, repaired_path),
                )
            else:
                plan.update(repaired)
            plan = _validate_ollama_plan(
                plan, company, industry, brief, evidence, opportunity_context
            )
    except (AgentError, json.JSONDecodeError, TypeError, ValueError) as error:
        logger.error("Ollama Delivery handoff generation or validation failed: %s", error)
        plan["generation_mode"] = "ollama"
        plan["generation_metadata"] = {
            "provider": "ollama",
            "model": model,
            "generation_status": "partial",
            "completed_section": "poc_plan",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "response_attempts": plan_attempts + plan_repair_attempts,
            "retrieval": "keyword_overlap_with_reviewer_feedback",
        }
        raise AgentError(
            f"Delivery handoff generation failed; the validated POC plan is preserved: {error}",
            partial_plan=plan,
        ) from error

    plan["generation_mode"] = "ollama"
    all_attempts = (
        plan_attempts
        + plan_repair_attempts
        + handoff_attempts
        + handoff_repair_attempts
    )
    final_metadata = handoff_attempts[-1] if handoff_attempts else {}
    plan["generation_metadata"] = {
        "provider": "ollama",
        "model": model,
        "base_url": base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validation_repair_used": bool(plan_repair_attempts or handoff_repair_attempts),
        "generation_timings": {
            "initial_seconds": plan_attempts[0]["elapsed_seconds"],
            "plan_retry_seconds": sum(
                attempt["elapsed_seconds"] for attempt in plan_attempts[1:]
            ),
            "handoff_seconds": handoff_attempts[0]["elapsed_seconds"],
            "handoff_retry_seconds": sum(
                attempt["elapsed_seconds"] for attempt in handoff_attempts[1:]
            ),
            "repair_seconds": sum(
                attempt["elapsed_seconds"]
                for attempt in plan_repair_attempts + handoff_repair_attempts
            ),
            "total_seconds": round(time.perf_counter() - generation_started, 3),
        },
        "request_sizes_bytes": {
            "initial": plan_attempts[0]["request_bytes"],
            "handoff": handoff_attempts[0]["request_bytes"],
            "repair": sum(
                attempt["request_bytes"]
                for attempt in plan_repair_attempts + handoff_repair_attempts
            ),
            "retry": sum(
                attempt["request_bytes"]
                for attempt in plan_attempts[1:] + handoff_attempts[1:]
            ),
        },
        "response_completion": {
            key: final_metadata.get(key)
            for key in (
                "done",
                "done_reason",
                "prompt_eval_count",
                "eval_count",
                "total_duration",
            )
        },
        "response_attempts": all_attempts,
        "retrieval": "keyword_overlap_with_reviewer_feedback",
    }
    return plan


def generate_plan(
    company,
    industry,
    brief,
    evidence,
    mode=None,
    model=None,
    base_url=None,
    opportunity_context=None,
):
    configured_mode = (mode or os.getenv("AGENT_MODE", "ollama")).strip().lower()
    if configured_mode in {"demo", "demo_fixture"}:
        plan = _fallback_plan(company, industry, brief, evidence)
        plan["customer_context"] = {
            "account": company,
            "opportunity_id": (opportunity_context or {}).get("opportunity_id"),
            "region": (opportunity_context or {}).get("region"),
            "owner": (opportunity_context or {}).get("owner"),
        }
        plan["generation_metadata"] = {
            "provider": "demo_fixture",
            "model": None,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "retrieval": "keyword_overlap_with_reviewer_feedback",
        }
        return plan
    if configured_mode != "ollama":
        raise AgentError(
            f"Unsupported AGENT_MODE '{configured_mode}'; use 'ollama' or explicit 'demo'."
        )
    return _ollama_plan(
        company,
        industry,
        brief,
        evidence,
        model or os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
        base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        opportunity_context,
    )
