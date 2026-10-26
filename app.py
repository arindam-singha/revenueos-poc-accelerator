import json
from pathlib import Path
import sqlite3

import streamlit as st

from agent import AgentError
from database import (
    get_plan,
    get_opportunity_events,
    get_opportunity_queue,
    get_recent_plans,
    get_review_decisions,
    get_sla_triggers,
    get_template_reuse_metrics,
    initialize_database,
    sync_sla_triggers,
)
from sla_monitor import get_business_day_age, get_sla_deadlines, get_sla_status
from workflow import create_plan, review_plan


BASE_DIR = Path(__file__).parent
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

st.title("RevenueOS POC Accelerator")
st.caption("Turn a customer brief into a practical, evidence-informed POC plan.")

opportunity_queue = get_opportunity_queue(DATABASE_PATH)
st.header("Opportunity queue")
if opportunity_queue:
    queue_display = []
    for event in opportunity_queue:
        queue_display.append(
            {
                "Opportunity": event["opportunity_id"],
                "Account": event["account"],
                "Region": event["region"],
                "Owner": event["owner"],
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
    st.caption(
        "Planning is available without an API key. Set OPENAI_API_KEY to enable "
        "optional LLM-generated plans."
    )

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
    submitted = st.form_submit_button("Generate POC plan", type="primary")

if submitted:
    if not company.strip() or not brief.strip() or not owner.strip() or not event_timestamp.strip():
        st.error("Enter a company, customer brief, owner, and original event timestamp.")
    else:
        try:
            with st.spinner("Building your POC plan..."):
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
        except (AgentError, OSError, ValueError, sqlite3.Error) as error:
            st.error(f"Plan generation failed: {error}")

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
        f'Plan ID {active_plan["id"]} · Reviewer role: **US Solution Architect**'
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

    left, right = st.columns(2)
    with left:
        st.subheader("Recommended scope")
        for item in plan["recommended_scope"]:
            st.markdown(f"- {item}")
        st.subheader("Success metrics")
        for item in plan["success_metrics"]:
            st.markdown(f"- {item}")
    with right:
        st.subheader("Delivery plan")
        for phase in plan["phases"]:
            with st.expander(f'{phase["name"]} · {phase["duration"]}', expanded=True):
                for task in phase["tasks"]:
                    st.markdown(f"- {task}")
                st.caption(f'Deliverable: {phase["deliverable"]}')

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
        for item in template_matches:
            score = item.get("score")
            score_text = f'{score:.3f}' if score is not None else "n/a"
            with st.expander(f'{item["title"]} · score {score_text}'):
                st.write(f'Match explanation: {item["reason"]}')
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

    if plan.get("delivery_handoff"):
        st.subheader("Delivery handoff")
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
            st.markdown(
                f'**{decision["decision"].title()}** by {decision["reviewer_role"]} '
                f'· {decision["timestamp"]}'
            )
            if details:
                st.caption(details)

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
st.header("Template reuse metrics")
reuse_metrics = get_template_reuse_metrics(DATABASE_PATH)
if not reuse_metrics:
    st.info("Template reuse metrics will appear after plans use retrieved templates.")
else:
    st.caption(
        "Counts are based on saved plan evidence. These are descriptive reuse metrics; "
        "review feedback does not automatically alter retrieval ranking."
    )
    st.dataframe(reuse_metrics, hide_index=True, use_container_width=True)
