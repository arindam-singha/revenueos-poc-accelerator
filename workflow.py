import os
from agent import AgentError, generate_plan
from pathlib import Path
from database import (
    add_workflow_event,
    get_template_ranking_feedback,
    initialize_database,
    record_review_decision,
    save_brief,
    save_plan,
)
from retrieval import retrieve_context

REVIEWER_ROLE = "US Solution Architect"


def create_plan(
    company,
    industry,
    brief,
    database_path,
    data_dir,
    event_timestamp,
    owner,
    opportunity_id=None,
    region=None,
    mode=None,
    model=None,
    base_url=None,
):
    initialize_database(database_path, Path(data_dir) / "briefs.json")
    brief_id = save_brief(
        database_path,
        company,
        industry,
        brief,
        event_timestamp=event_timestamp,
        owner=owner,
        opportunity_id=opportunity_id,
        region=region,
    )
    evidence = retrieve_context(
        query=f"{industry} {brief}",
        data_dir=data_dir,
        ranking_feedback=get_template_ranking_feedback(database_path),
    )
    opportunity_context = {
        "opportunity_id": opportunity_id,
        "region": region,
        "owner": owner,
        "event_timestamp": event_timestamp,
    }
    add_workflow_event(
        database_path,
        "plan_generation_started",
        opportunity_id=opportunity_id,
        brief_id=brief_id,
        payload={
            "mode": mode or os.getenv("AGENT_MODE", "ollama"),
            "model": model or os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
        },
    )
    try:
        plan = generate_plan(
            company=company,
            industry=industry,
            brief=brief,
            evidence=evidence,
            mode=mode,
            model=model,
            base_url=base_url,
            opportunity_context=opportunity_context,
        )
    except AgentError as error:
        partial_plan = error.partial_plan
        if partial_plan is not None:
            add_workflow_event(
                database_path,
                "plan_generation_partial",
                opportunity_id=opportunity_id,
                brief_id=brief_id,
                payload={"plan": partial_plan, "error": str(error)},
            )
        add_workflow_event(
            database_path,
            "plan_generation_failed",
            opportunity_id=opportunity_id,
            brief_id=brief_id,
            payload={"mode": mode, "error": str(error)},
        )
        raise
    plan_id = save_plan(database_path, brief_id, plan)
    return plan, plan_id


def review_plan(
    database_path,
    plan_id,
    decision,
    edited_plan=None,
    edit_details=None,
    rejection_reason=None,
    retained_template_ids=None,
):
    return record_review_decision(
        database_path=database_path,
        plan_id=plan_id,
        decision=decision,
        reviewer_role=REVIEWER_ROLE,
        edited_plan=edited_plan,
        edit_details=edit_details,
        rejection_reason=rejection_reason,
        retained_template_ids=retained_template_ids,
    )
