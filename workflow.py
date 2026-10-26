from agent import generate_plan
from database import (
    initialize_database,
    record_review_decision,
    save_brief,
    save_plan,
)
from retrieval import retrieve_context

REVIEWER_ROLE = "US Solution Architect"


def create_plan(company, industry, brief, database_path, data_dir, api_key=None):
    initialize_database(database_path)
    brief_id = save_brief(database_path, company, industry, brief)
    evidence = retrieve_context(
        query=f"{industry} {brief}",
        data_dir=data_dir,
    )
    plan = generate_plan(
        company=company,
        industry=industry,
        brief=brief,
        evidence=evidence,
        api_key=api_key,
    )
    plan_id = save_plan(database_path, brief_id, plan)
    return plan, plan_id


def review_plan(
    database_path,
    plan_id,
    decision,
    edited_plan=None,
    edit_details=None,
    rejection_reason=None,
):
    return record_review_decision(
        database_path=database_path,
        plan_id=plan_id,
        decision=decision,
        reviewer_role=REVIEWER_ROLE,
        edited_plan=edited_plan,
        edit_details=edit_details,
        rejection_reason=rejection_reason,
    )
