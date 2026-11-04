import json
import os
from pathlib import Path
import sqlite3

import streamlit as st
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

from agent import AgentError
from database import (
    get_plan,
    get_opportunity_events,
    get_opportunity_queue,
    get_recent_plans,
    get_review_decisions,
    get_sla_triggers,
    get_opportunity_sla_triggers,
    get_template_reuse_metrics,
    get_workflow_events,
    initialize_database,
    sync_sla_triggers,
)
from sla_monitor import get_business_day_age, get_sla_deadlines, get_sla_status
from workflow import create_plan, review_plan
from monitor_worker import start_sla_monitor


DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = BASE_DIR / "data" / "revenueos.db"


def load_selected_example():
    opportunity_id = st.session_state.queue_opportunity_id
    example = next(
        (
            event
            for event in get_opportunity_events(DATABASE_PATH)
            if event["opportunity_id"] == opportunity_id
        ),
        None,
    )
    if example is None:
        return
    st.session_state.company = example["account"]
    st.session_state.industry = "Financial services"
    st.session_state.brief = example["customer_brief"]
    st.session_state.opportunity_id = example["opportunity_id"]
    st.session_state.owner = example["owner"]
    st.session_state.region = example["region"]
    st.session_state.event_timestamp = example["event_timestamp"]


st.set_page_config(page_title="RevenueOS POC Accelerator", page_icon="🚀", layout="wide")
initialize_database(DATABASE_PATH, DATA_DIR / "briefs.json")
start_sla_monitor(DATABASE_PATH)

st.title("RevenueOS POC Accelerator")
st.caption("Turn a customer brief into a practical, evidence-informed POC plan.")

opportunity_queue = get_opportunity_queue(DATABASE_PATH)
st.header("Opportunity queue")
if opportunity_queue:
    new_crm_events = [
        event for event in opportunity_queue
        if event.get("source") != "seed" and not event.get("latest_plan_id")
    ]
    st.metric("New CRM opportunities awaiting pickup", len(new_crm_events))
    queue_display = []
    for event in opportunity_queue:
        queue_display.append(
            {
                "Opportunity": event["opportunity_id"],
                "Account": event["account"],
                "Region": event["region"],
                "Owner": event["owner"],
                "Source": event.get("source", "seed"),
                "Intake": "NEW" if not event.get("latest_plan_id") else "POC Started",
                "Event time": event["event_timestamp"],
                "Business-day age": get_business_day_age(event["event_timestamp"]),
                "SLA": get_sla_status(
                    event["event_timestamp"],
                    first_pickup_at=event["first_pickup_at"],
                ),
                "Plan": event["latest_plan_status"] or "Not started",
            }
        )
    st.dataframe(queue_display, hide_index=True, use_container_width=True)
    opportunity_ids = [event["opportunity_id"] for event in opportunity_queue]
    event_by_id = {
        event["opportunity_id"]: event for event in opportunity_queue
    }
    st.selectbox(
        "Opportunity details",
        options=opportunity_ids,
        format_func=lambda opportunity_id: (
            f'{opportunity_id} · {event_by_id[opportunity_id]["account"]}'
        ),
        key="queue_opportunity_id",
    )
    selected_event = event_by_id[st.session_state.queue_opportunity_id]
    details_column, load_column = st.columns([4, 1])
    with details_column:
        st.write(
            f'**{selected_event["account"]}** · {selected_event["region"]} · '
            f'Owner: {selected_event["owner"]} · '
            f'Event: {selected_event["event_timestamp"]} · '
            f'{selected_event["event_type"]} ({selected_event["event_id"]})'
        )
        st.write(selected_event["customer_brief"])
        st.caption(
            f"Source: {selected_event.get('source', 'seed')} · "
            f"Segment: {selected_event.get('segment') or 'Not supplied'} · "
            f"Regulator: {selected_event.get('regulator') or 'Not supplied'}"
        )
        if selected_event.get("systems_json"):
            st.write("**Systems:** " + ", ".join(json.loads(selected_event["systems_json"])))
        if selected_event.get("expected_timeline"):
            st.write("**Expected timeline:** " + selected_event["expected_timeline"])
        if selected_event.get("success_criteria_json"):
            st.write("**Success criteria:** " + "; ".join(json.loads(selected_event["success_criteria_json"])))
    with load_column:
        st.button(
            "Load brief",
            on_click=load_selected_example,
            use_container_width=True,
        )
else:
    st.info("No opportunity events are available.")

with st.sidebar:
    st.subheader("Plan generation")
    st.divider()
    agent_mode = os.getenv("AGENT_MODE", "ollama").strip().lower()
    if agent_mode == "ollama":
        st.success(
            "Agent mode: Ollama · "
            + os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
        )
        st.caption(
            f'Local API: {os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")}. '
            "Generation errors are surfaced; no offline result is substituted."
        )
    elif agent_mode in {"demo", "demo_fixture"}:
        st.warning(
            "Demo fixture mode is active. Plans are not generated by an AI model."
        )
    else:
        st.error(f"Unsupported AGENT_MODE: {agent_mode}")

with st.form("brief_form"):
    st.subheader("Customer brief")
    company = st.text_input(
        "Account",
        key="company",
        placeholder="Acme Corp",
    )
    industry = st.text_input(
        "Industry", key="industry", placeholder="B2B SaaS"
    )
    brief = st.text_area(
        "What is the customer trying to achieve?",
        key="brief",
        height=180,
        placeholder=(
            "Describe the business problem, current workflow, systems involved, "
            "desired outcomes, and any constraints."
        ),
    )
    opportunity_id = st.text_input("Opportunity ID", key="opportunity_id")
    owner = st.text_input(
        "Opportunity owner",
        key="owner",
        placeholder="Required for SLA ownership",
    )
    region = st.text_input("Region", key="region")
    event_timestamp = st.text_input(
        "Original opportunity event timestamp (ISO 8601 with timezone)",
        key="event_timestamp",
        placeholder="2026-10-26T08:30:00Z",
        help="The SLA starts from this event timestamp, not from plan creation.",
    )
    submitted = st.form_submit_button(
        "Generate POC plan",
        type="primary",
        disabled=st.session_state.get("plan_generation_in_progress", False),
    )

if submitted:
    if st.session_state.get("plan_generation_in_progress", False):
        st.info("A plan generation request is already in progress.")
    elif not company.strip() or not brief.strip() or not owner.strip() or not event_timestamp.strip():
        st.error("Enter a company, customer brief, owner, and original event timestamp.")
    else:
        st.session_state["plan_generation_in_progress"] = True
        try:
            with st.status(
                "Generating your POC plan with Ollama. This may take several minutes (up to 13 minutes).",
                expanded=True,
            ) as generation_status:
                generation_status.write(
                    "Waiting for the local model; this screen prevents repeat submissions."
                )
                plan, plan_id = create_plan(
                    company=company.strip(),
                    industry=industry.strip() or "General",
                    brief=brief.strip(),
                    database_path=DATABASE_PATH,
                    data_dir=DATA_DIR,
                    event_timestamp=event_timestamp.strip(),
                    owner=owner.strip(),
                    opportunity_id=opportunity_id.strip() or None,
                    region=region.strip() or None,
                )
            st.session_state["active_plan_id"] = plan_id
            generation_status.update(
                label="POC plan generated and saved.",
                state="complete",
                expanded=False,
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as error:
            st.error(
                f"Plan generation failed ({agent_mode} mode): {error}. "
                "Review the reported failure and retry when the local model is ready."
            )
        finally:
            st.session_state["plan_generation_in_progress"] = False

sync_sla_triggers(DATABASE_PATH)
recent_plans = get_recent_plans(DATABASE_PATH, limit=10)
if recent_plans:
    plan_ids = [item["id"] for item in recent_plans]
    if st.session_state.get("active_plan_id") not in plan_ids:
        st.session_state["active_plan_id"] = plan_ids[0]
    plan_labels = {
        item["id"]: f'{item["company"]} — {item["title"]} [{item["status"]}]'
        for item in recent_plans
    }
    active_plan_id = st.selectbox(
        "Current POC plan",
        options=plan_ids,
        format_func=lambda plan_id: plan_labels[plan_id],
        key="active_plan_id",
    )
    active_plan = get_plan(DATABASE_PATH, active_plan_id)
else:
    active_plan = None

if active_plan:
    plan = active_plan["plan"]
    st.divider()
    st.header(f'{plan["title"]} · {active_plan["status"].title()}')
    st.write(plan["executive_summary"])
    st.caption(
        f'Plan ID {active_plan["id"]} · Reviewer role: **US Solution Architect** · '
        + (
            f'Ollama model: {plan.get("generation_metadata", {}).get("model")}'
            if plan.get("generation_mode") == "ollama"
            else "Explicit demo fixture — not AI-generated"
            if plan.get("generation_mode") == "demo_fixture"
            else "Unrecognized generation mode"
        )
    )
    if active_plan["event_timestamp"] and active_plan["owner"]:
        amber_at, due_at = get_sla_deadlines(active_plan["event_timestamp"])
        sla_status = get_sla_status(
            active_plan["event_timestamp"],
            first_pickup_at=next(
                (
                    decision["timestamp"]
                    for decision in reversed(
                        get_review_decisions(DATABASE_PATH, active_plan["id"])
                    )
                ),
                None,
            ),
        )
        st.metric("Pickup SLA", sla_status)
        st.caption(
            f'Original event: {active_plan["event_timestamp"]} · '
            f'Owner: {active_plan["owner"]} · '
            f'Business-day age: {get_business_day_age(active_plan["event_timestamp"])} · '
            f'Amber at {amber_at.isoformat(timespec="minutes")} ET · '
            f'Due {due_at.isoformat(timespec="minutes")} ET'
        )
    else:
        st.warning(
            "SLA unavailable: this legacy brief has no original event timestamp "
            "or owner. Plan creation time is not used as a substitute."
        )

    if plan.get("proposed_solution"):
        st.subheader("Proposed solution")
        st.write(plan["proposed_solution"])
    if plan.get("business_problem") or plan.get("objectives"):
        problem_column, objective_column = st.columns(2)
        with problem_column:
            st.subheader("Business problem")
            st.write(plan.get("business_problem", active_plan["brief_text"]))
        with objective_column:
            st.subheader("Objectives")
            for item in plan.get("objectives", []):
                st.markdown(f"- {item}")
    if plan.get("architecture_outline"):
        st.subheader("Architecture outline")
        st.write(plan["architecture_outline"])
    if plan.get("facts_from_brief"):
        with st.expander("Facts cited from the customer brief"):
            for fact in plan["facts_from_brief"]:
                if isinstance(fact, dict):
                    st.markdown(f'- **Fact:** {fact.get("fact", "")}')
                    st.caption(f'Brief evidence: “{fact.get("evidence_quote", "")}”')
                else:
                    st.markdown(f"- {fact}")

    left, right = st.columns(2)
    with left:
        st.subheader("Recommended scope")
        for item in plan["recommended_scope"]:
            st.markdown(f"- {item}")
        st.subheader("Success metrics")
        for item in plan["success_metrics"]:
            st.markdown(f"- {item}")
        if plan.get("acceptance_criteria"):
            st.subheader("Acceptance criteria")
            for item in plan["acceptance_criteria"]:
                st.markdown(f"- {item}")
    with right:
        st.subheader("Delivery plan")
        for phase in plan["phases"]:
            with st.expander(f'{phase["name"]} · {phase["duration"]}', expanded=True):
                for task in phase["tasks"]:
                    st.markdown(f"- {task}")
                st.caption(f'Deliverable: {phase["deliverable"]}')
    if plan.get("exclusions") or plan.get("deliverables"):
        exclusions_column, deliverables_column = st.columns(2)
        with exclusions_column:
            st.subheader("Out of scope")
            for item in plan.get("exclusions", []):
                st.markdown(f"- {item}")
        with deliverables_column:
            st.subheader("Key deliverables")
            for item in plan.get("deliverables", []):
                st.markdown(f"- {item}")

    left, right = st.columns(2)
    with left:
        st.subheader("Risks and mitigations")
        for item in plan["risks"]:
            st.markdown(f"- {item}")
    with right:
        st.subheader("Assumptions and next steps")
        for item in plan["assumptions"]:
            st.markdown(f"- {item}")
        for item in plan["next_steps"]:
            st.markdown(f"- {item}")

    template_matches = [
        item for item in plan.get("evidence", [])
        if item.get("type") == "template"
    ]
    st.subheader("Retrieved templates")
    if template_matches:
        explanation_by_id = {
            item.get("template_id"): item
            for item in plan.get("template_match_explanations", [])
        }
        for item in template_matches:
            score = item.get("score")
            score_text = f'{score:.3f}' if score is not None else "n/a"
            with st.expander(f'{item["title"]} · score {score_text}'):
                st.write(f'Match explanation: {item["reason"]}')
                if item.get("ranking_adjustment"):
                    st.caption(
                        f'Lexical score {item.get("base_score", score):.3f}; '
                        f'reviewer-feedback adjustment '
                        f'{item["ranking_adjustment"]:+.3f}; adjusted score {score:.3f}.'
                    )
                match_explanation = explanation_by_id.get(item.get("id"))
                if match_explanation:
                    st.write("Agent explanation:", match_explanation.get("reason"))
                    st.write(
                        "Cited template fields:",
                        match_explanation.get("cited_fields", {}),
                    )
        if plan.get("template_reuse"):
            with st.expander("AI template reuse analysis"):
                st.json(plan["template_reuse"])
                template = item.get("content")
                if template:
                    st.write(template.get("description", ""))
                    if template.get("required_capabilities"):
                        st.markdown("**Required capabilities**")
                        for capability in template["required_capabilities"]:
                            st.markdown(f"- {capability}")
                    if template.get("reusable_components"):
                        st.markdown("**Reusable components**")
                        for component in template["reusable_components"]:
                            st.markdown(f"- {component}")
    else:
        st.info("No reusable template matched this plan.")

    example_matches = [
        item for item in plan.get("evidence", [])
        if item.get("type") == "example"
    ]
    if example_matches:
        with st.expander("Related opportunity examples"):
            for item in example_matches:
                score = item.get("score")
                score_text = f" · score {score:.3f}" if score is not None else ""
                st.markdown(f'**{item["title"]}**{score_text} — {item["reason"]}')

    if plan.get("required_data_integrations"):
        st.subheader("Required data and integrations")
        for item in plan["required_data_integrations"]:
            st.markdown(f"- {item}")
    if plan.get("dependencies"):
        st.subheader("Dependencies")
        for item in plan["dependencies"]:
            st.markdown(f"- {item}")

    if plan.get("delivery_handoff"):
        st.subheader("Delivery handoff")
        if isinstance(plan["delivery_handoff"], dict):
            with st.expander("Engineering handoff details", expanded=True):
                st.json(plan["delivery_handoff"])
        else:
            for item in plan["delivery_handoff"]:
                st.markdown(f"- {item}")

    st.download_button(
        "Download plan as JSON",
        data=json.dumps(plan, indent=2),
        file_name="poc-plan.json",
        mime="application/json",
    )

    st.subheader("US Solution Architect review")
    accept_column, edit_column, reject_column = st.columns(3)
    with accept_column:
        with st.form(f"accept_plan_{active_plan_id}"):
            st.write("Approve this plan for customer-facing use.")
            accept_submitted = st.form_submit_button("Accept plan")
        if accept_submitted:
            try:
                review_plan(DATABASE_PATH, active_plan_id, "accept")
                st.rerun()
            except (ValueError, sqlite3.Error) as error:
                st.error(f"Could not accept plan: {error}")

    with edit_column:
        with st.form(f"edit_plan_{active_plan_id}"):
            edited_plan_json = st.text_area(
                "Edit plan JSON",
                value=json.dumps(plan, indent=2),
                height=220,
                key=f"edited_plan_json_{active_plan_id}",
            )
            edit_details = st.text_input(
                "Edit summary",
                key=f"edit_details_{active_plan_id}",
            )
            edit_submitted = st.form_submit_button("Save edited plan")
        if edit_submitted:
            try:
                edited_plan = json.loads(edited_plan_json)
                review_plan(
                    DATABASE_PATH,
                    active_plan_id,
                    "edit",
                    edited_plan=edited_plan,
                    edit_details=edit_details,
                    retained_template_ids=[
                        item["id"]
                        for item in edited_plan.get("evidence", [])
                        if item.get("type") == "template" and item.get("id")
                    ],
                )
                st.rerun()
            except (json.JSONDecodeError, ValueError, sqlite3.Error) as error:
                st.error(f"Could not save edited plan: {error}")

    with reject_column:
        with st.form(f"reject_plan_{active_plan_id}"):
            rejection_reason = st.text_area(
                "Reason for rejection",
                key=f"rejection_reason_{active_plan_id}",
            )
            reject_submitted = st.form_submit_button("Reject plan")
        if reject_submitted:
            try:
                review_plan(
                    DATABASE_PATH,
                    active_plan_id,
                    "reject",
                    rejection_reason=rejection_reason,
                )
                st.rerun()
            except (ValueError, sqlite3.Error) as error:
                st.error(f"Could not reject plan: {error}")

    decisions = get_review_decisions(DATABASE_PATH, active_plan_id)
    st.subheader("Decision history")
    if not decisions:
        st.info("No reviewer decision has been recorded for this plan.")
    else:
        for decision in decisions:
            details = decision["edit_details"] or decision["rejection_reason"]
            retained_ids = json.loads(
                decision.get("retained_template_ids_json") or "[]"
            )
            st.markdown(
                f'**{decision["decision"].title()}** by {decision["reviewer_role"]} '
                f'· {decision["timestamp"]} · Opportunity '
                f'{decision.get("opportunity_id") or "unlinked"}'
            )
            if details:
                st.caption(details)
            if retained_ids:
                st.caption("Templates retained: " + ", ".join(retained_ids))

st.divider()
st.header("Recent POC plans")
if not recent_plans:
    st.info("Your generated plans will appear here.")
else:
    for item in recent_plans:
        if item["event_timestamp"] and item["owner"]:
            status = get_sla_status(
                item["event_timestamp"],
                first_pickup_at=item["first_pickup_at"],
            )
            age = f'{get_business_day_age(item["event_timestamp"])} business day(s)'
        else:
            status = "Unavailable — missing original event timestamp or owner"
            age = "unknown age"
        st.markdown(
            f'**{item["company"]}** · {item["title"]} · '
            f'{item["event_timestamp"]} · Owner: {item["owner"]} · '
            f'Age: {age} · SLA: **{status}**'
        )

st.divider()
st.header("SLA breach trigger log")
triggers = get_sla_triggers(DATABASE_PATH)
if not triggers:
    st.info("No pickup SLA breaches have been triggered.")
else:
    for trigger in triggers:
        with st.expander(
            f'Plan {trigger["plan_id"]} · Brief {trigger["brief_id"]} · '
            f'{trigger["status"]} · Owner: {trigger["owner"]}'
        ):
            st.write(f'Breach timestamp: {trigger["breach_timestamp"]}')
            st.write(f'Draft reference: {trigger["current_draft_reference"]}')
            st.json(json.loads(trigger["draft_json"]))

st.divider()
st.header("Automatic opportunity SLA escalations")
opportunity_triggers = get_opportunity_sla_triggers(DATABASE_PATH)
if not opportunity_triggers:
    st.info("No opportunity-level SLA breaches have been triggered.")
else:
    for trigger in opportunity_triggers:
        with st.expander(
            f"{trigger['opportunity_id']} · {trigger['status']} · "
            f"Owner: {trigger['owner']} · Due: {trigger['breach_timestamp']}"
        ):
            st.write(f"Account: {trigger['payload'].get('account')}")
            st.write(trigger['payload'].get('customer_brief', ''))
            st.write(f"Notification: {'Sent' if trigger.get('notified_at') else 'Pending / failed'}")
            if trigger.get('notification_error'):
                st.warning(trigger['notification_error'])
            st.json(trigger['payload'])

st.divider()
st.header("Template reuse metrics")
reuse_metrics = get_template_reuse_metrics(DATABASE_PATH)
if not reuse_metrics:
    st.info("Template reuse metrics will appear after plans use retrieved templates.")
else:
    st.caption(
        "Draft use counts reflect templates included in generated plans. Retained counts "
        "reflect templates kept by the latest accept/edit decision for each plan. "
        "The transparent ranking adjustment is +0.05 per latest explicitly accepted "
        "retained template and -0.05 per latest rejected candidate, capped at +/-0.20. "
        "Editing alone is not acceptance and does not change ranking; this signal changes "
        "retrieval ordering only and does not train a model."
    )
    st.dataframe(reuse_metrics, hide_index=True, use_container_width=True)

st.divider()
st.header("Workflow event log")
workflow_events = get_workflow_events(DATABASE_PATH, limit=30)
if not workflow_events:
    st.info("Workflow events will appear after generation or reviewer actions.")
else:
    event_rows = [
        {
            "When": event["timestamp"],
            "Event": event["event_type"],
            "Opportunity": event["opportunity_id"],
            "Plan": event["plan_id"],
            "Details": json.dumps(event["payload"], ensure_ascii=False),
        }
        for event in workflow_events
    ]
    st.dataframe(event_rows, hide_index=True, use_container_width=True)
