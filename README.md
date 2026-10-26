# RevenueOS POC Accelerator

A small Streamlit MVP that turns a customer brief into a structured, evidence-informed POC plan. The core workflow runs locally without an LLM credential; configuring `OPENAI_API_KEY` enables optional OpenAI-compatible generation.

## Features

- Brief intake with sample customer briefs.
- Idempotent SQLite seeding of synthetic won-opportunity events with opportunity ID, original event timestamp, owner, account, region, and customer brief.
- Lightweight keyword-overlap retrieval over sample briefs and POC templates.
- Deterministic, structured plan generation when no API key is configured.
- Optional LLM-generated plan using an OpenAI-compatible Chat Completions endpoint.
- SQLite storage for opportunity event timestamps, plans, review decisions, and SLA breach triggers.
- Two-business-day pickup SLA calculated in US Eastern time with Green, Amber, and Red statuses.
- US Solution Architect accept, edit, and reject actions, with timestamped SQLite decision history.
- One-screen opportunity queue, full retrieved-template match details, SLA trigger log, and template reuse/acceptance counts.
- JSON plan download and recent-plan list.

## Quick start

```powershell
cd revenueos-poc-accelerator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

The app creates `data/revenueos.db` and seeds synthetic opportunity events from `data/briefs.json` on first run. Repeated initialization does not duplicate seeded opportunity IDs. Selecting a sample loads its source event details into the intake form; for seeded opportunities, account, owner, region, and event timestamp must match the stored source event. To use LLM generation, set `OPENAI_API_KEY` in the environment before starting Streamlit. `OPENAI_MODEL` and `OPENAI_BASE_URL` can optionally override the defaults. API or invalid-response errors are surfaced in the UI; they are not silently presented as successful fallback output.

### Pickup SLA

The SLA clock starts at the opportunity's original ISO 8601 event timestamp, including its timezone offset; plan creation time is never substituted. Timestamps are converted to US Eastern time (`America/New_York`). The pickup deadline is two business days after the event, preserving its Eastern local time and skipping Saturdays and Sundays. **Green** means less than one business day has elapsed or the plan was picked up by the deadline; **Amber** means at least one business day has elapsed and the deadline has not been reached; **Red** means the deadline passed without an on-time reviewer decision. The first persisted US Solution Architect review decision (accept, edit, or reject) counts as pickup. Breach triggers are stored once per plan with owner, breach time, and a snapshot/reference to the current draft; refreshing the dashboard does not duplicate them. This prototype excludes weekends but does not account for US public holidays.

New plans require both an opportunity owner and an explicit timezone-aware original event timestamp. Legacy database rows without these fields show SLA as unavailable rather than deriving it from plan creation.

Generate a plan, select it from the current-plan list, then use the US Solution Architect review controls to accept it, save edited plan JSON, or reject it with a reason. The plan status and timestamped decision history are reloaded from SQLite after each action.

The opportunity queue shows each synthetic event, owner, region, original event time, business-day age, SLA status, and plan state. Select a queue item and load it into the brief form. Retrieved templates include their relevance score, match explanation, capabilities, and reusable components. Template reuse metrics count references in saved plan evidence and accepted/rejected plan outcomes; they are descriptive and do not automatically change retrieval rankings.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Project layout

```text
app.py                 Streamlit UI
database.py            SQLite schema and persistence
retrieval.py           Keyword-overlap context retrieval
agent.py               Optional LLM and deterministic fallback generator
workflow.py            Brief-to-plan orchestration
sla_monitor.py         SLA status calculation
data/                  Example briefs and reusable plan templates
tests/                 Core workflow tests
docs/                  Build notes and MVP boundaries
```
