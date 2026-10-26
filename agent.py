import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AgentError(RuntimeError):
    """Raised when an enabled LLM provider cannot return a usable plan."""


def _template_matches(evidence):
    return [
        item
        for item in evidence
        if item.get("type") == "template"
        and isinstance(item.get("content"), dict)
        and item.get("score", 0) > 0
    ][:3]


def _evidence_items(evidence):
    return [
        {
            "id": item.get("id"),
            "type": item.get("type"),
            "title": item["title"],
            "reason": item["reason"],
            "score": round(item["score"], 3),
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
    return {
        "title": f"{company} — {industry} POC plan",
        "executive_summary": (
            f"Validate a focused, measurable solution for {company} by testing "
            f"{', '.join(scope)} against the outcomes in the customer brief. "
            "Start with a bounded data sample and agree on success criteria "
            "before building."
        ),
        "proposed_solution": proposed_solution,
        "recommended_scope": recommended_scope,
        "required_data_integrations": required_data_integrations,
        "phases": [
            {
                "name": "Align and qualify",
                "duration": "Days 1–2",
                "tasks": [
                    "Confirm the business owner, users, current workflow, and baseline.",
                    "Agree on data access, security constraints, and measurable acceptance criteria.",
                ] + implementation_tasks,
                "deliverable": "Signed-off POC brief and success scorecard",
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
            },
            {
                "name": "Pilot and decide",
                "duration": "Days 8–10",
                "tasks": [
                    "Run the pilot against agreed scenarios and compare results to baseline.",
                    "Review adoption, outcome metrics, open risks, and scale requirements.",
                ],
                "deliverable": "POC readout and go / iterate / stop recommendation",
            },
        ],
        "success_metrics": [
            "At least 80% of agreed pilot scenarios complete successfully.",
            "Measure change against an agreed baseline for the primary business outcome.",
            "Pilot users rate workflow usefulness at least 4/5.",
            "No unresolved critical data, security, or access-control issues.",
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
        "next_steps": [
            "Schedule a 45-minute discovery and success-criteria workshop.",
            "Request a sample dataset, system diagram, and security requirements.",
            "Name customer and delivery owners and confirm pilot participants.",
        ],
        "delivery_handoff": delivery_handoff,
        "evidence": evidence_items,
        "generation_mode": "deterministic_fallback",
    }


def _llm_plan(company, industry, brief, evidence, api_key, model, base_url):
    prompt = {
        "company": company,
        "industry": industry,
        "customer_brief": brief,
        "retrieved_playbook_context": [
            {"title": item["title"], "content": item["content"]}
            for item in evidence
        ],
        "requirements": (
            "Return only a valid JSON object with keys title, executive_summary, "
            "recommended_scope (array of strings), phases (array of objects with "
            "name, duration, tasks (array), deliverable), success_metrics (array), "
            "risks (array), assumptions (array), next_steps (array). Be concrete, "
            "keep the POC bounded, and do not invent customer facts."
        ),
    }
    request_data = json.dumps(
        {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a pragmatic GTM engineering POC planner.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = Request(
        base_url,
        data=request_data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise AgentError(f"LLM request failed: {error}") from error

    try:
        content = payload["choices"][0]["message"]["content"]
        plan = json.loads(content)
        required = {
            "title", "executive_summary", "recommended_scope", "phases",
            "success_metrics", "risks", "assumptions", "next_steps",
        }
        if not isinstance(plan, dict) or not required.issubset(plan):
            raise ValueError("response is missing required plan fields")
        plan["evidence"] = _evidence_items(evidence)
        plan["generation_mode"] = "llm"
        return plan
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AgentError(f"LLM returned an invalid plan: {error}") from error


def generate_plan(company, industry, brief, evidence, api_key=None, model=None, base_url=None):
    configured_key = os.getenv("OPENAI_API_KEY") if api_key is None else api_key
    if not configured_key:
        return _fallback_plan(company, industry, brief, evidence)
    return _llm_plan(
        company=company,
        industry=industry,
        brief=brief,
        evidence=evidence,
        api_key=configured_key,
        model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=base_url
        or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions"),
    )
