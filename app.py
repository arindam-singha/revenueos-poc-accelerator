import json
from pathlib import Path
import sqlite3

import streamlit as st

from agent import AgentError
from database import (
    get_plan,
    get_recent_plans,
    get_review_decisions,
    get_sla_triggers,
    initialize_database,
    sync_sla_triggers,
)
from sla_monitor import get_business_day_age, get_sla_deadlines, get_sla_status
from workflow import create_plan, review_plan


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = BASE_DIR / "data" / "revenueos.db"


def load_examples():
    with (DATA_DIR / "briefs.json").open(encoding="utf-8") as file:
        return json.load(file)


def load_selected_example():
    example = st.session_state.example_id
    if example:
        st.session_state.company = example["company"]
        st.session_state.industry = example["industry"]
        st.session_state.brief = example["brief"]
        st.session_state.opportunity_id = example["opportunity_id"]
        st.session_state.owner = example["owner"]
        st.session_state.region = example["region"]
        st.session_state.event_timestamp = example["timestamp"]


st.set_page_config(page_title="RevenueOS POC Accelerator", page_icon="🚀", layout="wide")
initialize_database(DATABASE_PATH)

st.title("RevenueOS POC Accelerator")
st.caption("Turn a customer brief into a practical, evidence-informed POC plan.")

with st.sidebar:
    st.subheader("Load a sample brief")
    examples = load_examples()
    st.selectbox(
        "Example",
        options=examples,
        format_func=lambda example: f'{example["company"]} — {example["industry"]}',
        key="example_id",
        index=None,
    )
    st.button("Load selected example", on_click=load_selected_example)
    st.divider()
    st.caption(
        "Planning is available without an API key. Set OPENAI_API_KEY to enable "
        "optional LLM-generated plans."
    )

with st.form("brief_form"):
    st.subheader("Customer brief")
    company = st.text_input("Company", key="company", placeholder="Acme Corp")
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

    if plan["evidence"]:
        with st.expander("Retrieved playbook references"):
            for item in plan["evidence"]:
                score = item.get("score")
                score_text = f" · relevance {score:.3f}" if score is not None else ""
                st.markdown(
                    f'**{item["title"]}**{score_text} — {item["reason"]}'
                )

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
