# RevenueOS POC Accelerator

A Streamlit MVP that turns a synthetic financial-services opportunity into an Ollama-generated, evidence-grounded POC plan and separate engineering handoff. Generation defaults to the local Ollama model; static planning is available only through an explicitly selected demo-fixture mode.

## Features

- Generate evidence-grounded POC plans and Delivery handoffs with a local Ollama model.
- Retrieve relevant templates and examples, with transparent reviewer-feedback ranking.
- Ingest opportunity-won events through an authenticated CRM webhook.
- Track a two-business-day pickup SLA and send breach notifications to the console or optionally Slack.
- Review, edit, accept, or reject plans; persist decisions and reuse metrics in SQLite.
- Use an explicitly selected demo fixture for UI demonstrations; it is not substituted for failed model generation.

## Prerequisites and setup

- Python 3.10+ and the project dependencies.
- Ollama running at `http://localhost:11434` with the configured model available. The project does not download models.

```powershell
cd revenueos-poc-accelerator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
curl.exe http://localhost:11434/api/tags
```

Create a local configuration from the example:

```powershell
Copy-Item .env.example .env
# Edit .env to configure your local model or integrations.
streamlit run app.py
```

The default mode is `ollama`. Set `OLLAMA_BASE_URL` or `OLLAMA_MODEL` in `.env` if needed. Keep credentials and local settings out of version control.

## Connect a CRM webhook

This step is optional. Start the webhook API in a separate terminal:

```powershell
uvicorn crm_ingestion:app --host 0.0.0.0 --port 8001
```

Set a strong `CRM_WEBHOOK_TOKEN` in `.env` and send it in the `X-RevenueOS-Token` header. The endpoint rejects requests if no token is configured; anonymous access is available only when `CRM_WEBHOOK_ALLOW_ANONYMOUS=true` is explicitly enabled for local testing. Example:

```powershell
$env:CRM_WEBHOOK_TOKEN = "your-same-secret-from-.env"
$event = @{
  event_id = "crm-event-2026-0001"
  timestamp = "2026-11-02T09:30:00-05:00"
  owner = "Jordan Lee"
  account = "Example Community Bank"
  opportunity_id = "OPP-CRM-0001"
  region = "US"
  customer_brief = "Automate onboarding document review with analyst approval and auditability."
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/v1/events/opportunity-won `
  -Headers @{ "X-RevenueOS-Token" = $env:CRM_WEBHOOK_TOKEN } `
  -ContentType "application/json" -Body $event
```

The first delivery returns `created: true`; a replay with the same `event_id` is reported as a duplicate. Check `GET http://localhost:8001/health` for API health. The webhook accepts a normalized `opportunity_won` event; map your CRM's native payload to this format. Vendor-specific CRM authentication/connectors are not included.

The app seeds sample opportunities on startup. Select an opportunity, load its brief, then click **Generate POC plan**. Generated plans are saved as drafts for human review. For a UI demonstration without Ollama, explicitly select demo mode in a separate shell:

```powershell
$env:AGENT_MODE = "demo"
streamlit run app.py
```

Demo plans are labeled as not AI-generated. Set `AGENT_MODE=ollama` to use the model again.

## Opportunity, review, and SLA behavior

CRM events are persisted in SQLite and deduplicated by `event_id`. The SLA monitor uses the opportunity's timezone-aware event timestamp, counts business days in US Eastern time, and sends a breach notification to the console; set `SLACK_WEBHOOK_URL` for Slack delivery. Optional holidays can be supplied through `SLA_HOLIDAYS`.

Plans remain drafts until reviewed. Accept, edit, and reject decisions are recorded; accepted and rejected plans affect template ranking, while edits alone do not.

## Tests

Run all unit tests; Ollama HTTP calls are mocked:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app.py agent.py retrieval.py workflow.py database.py sla_monitor.py crm_ingestion.py monitor_worker.py tests
```

The tests mock model requests and do not require a running Ollama service.

## Troubleshooting

- **Connection or model error:** verify Ollama is running, `OLLAMA_BASE_URL` is reachable, and `OLLAMA_MODEL` is installed.
- **Webhook requests are rejected:** configure `CRM_WEBHOOK_TOKEN` and send it in the `X-RevenueOS-Token` header.
- **Demo output shown:** check `AGENT_MODE`; demo plans are explicitly labeled as not AI-generated.

## Project layout

```text
app.py                 Streamlit UI
crm_ingestion.py       FastAPI CRM webhook endpoint
monitor_worker.py      Background SLA polling and notifications
database.py            SQLite persistence and workflow events
retrieval.py           Keyword-overlap template/example retrieval
agent.py               Ollama client, validation, and explicit demo fixture
workflow.py            Brief-to-plan orchestration
sla_monitor.py         Two-business-day SLA calculation
data/                  Synthetic opportunity events and solution templates
tests/                 Mocked agent and core workflow tests
docs/                  Build notes, cut list, and human-judgment boundary
```
