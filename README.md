# RevenueOS POC Accelerator

A small Streamlit MVP that turns a customer brief into a structured, evidence-informed POC plan. The core workflow runs locally without an LLM credential; configuring `OPENAI_API_KEY` enables optional OpenAI-compatible generation.

## Features

- Brief intake with sample customer briefs.
- Lightweight keyword-overlap retrieval over sample briefs and POC templates.
- Deterministic, structured plan generation when no API key is configured.
- Optional LLM-generated plan using an OpenAI-compatible Chat Completions endpoint.
- SQLite storage for submitted briefs and plans, with a four-hour SLA status indicator.
- US Solution Architect accept, edit, and reject actions, with timestamped SQLite decision history.
- JSON plan download and recent-plan list.

## Quick start

```powershell
cd revenueos-poc-accelerator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

The app creates `data/revenueos.db` on first run. To use LLM generation, set `OPENAI_API_KEY` in the environment before starting Streamlit. `OPENAI_MODEL` and `OPENAI_BASE_URL` can optionally override the defaults. API or invalid-response errors are surfaced in the UI; they are not silently presented as successful fallback output.

Generate a plan, select it from the current-plan list, then use the US Solution Architect review controls to accept it, save edited plan JSON, or reject it with a reason. The plan status and timestamped decision history are reloaded from SQLite after each action.

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
