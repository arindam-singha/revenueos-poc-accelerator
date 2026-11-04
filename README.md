# RevenueOS POC Accelerator

A Streamlit MVP that turns a synthetic financial-services opportunity into an Ollama-generated, evidence-grounded POC plan and separate engineering handoff. Generation defaults to the local Ollama model; static planning is available only through an explicitly selected demo-fixture mode.

## Features

- Idempotent SQLite seeding plus live CRM webhook ingestion for opportunity-won events.
- Authenticated FastAPI webhook with event-ID idempotency and persisted CRM metadata.
- Independent background SLA polling, durable opportunity-level escalations, console delivery, and optional Slack webhook notification.
- Keyword-overlap retrieval with match explanations and reviewer-feedback ranking adjustments.
- Real local model generation through Ollama's `POST /api/chat` endpoint.
- Structured JSON plan and handoff validation, including template ID and component checks.
- POC plans and Delivery handoffs are generated in separate bounded calls.
- `expected_workflow` must be a non-empty array; missing or invalid fields are never fabricated.
- Length-truncated calls get one higher-budget retry; a valid POC plan is retained if handoff generation fails.
- Generation timing, Ollama completion status, and validation errors are logged without customer briefs.
- The dashboard shows an in-progress status and disables repeat submission during generation.
- Persistent plan, generation/failure events, reviewer decisions, SLA breach triggers, and reuse metrics.
- US Solution Architect accept/edit/reject controls on one screen.
- Two-business-day pickup SLA in US Eastern time.

## Prerequisites and setup

- Python 3.10+ and the project dependencies.
- An Ollama service reachable from this machine at `http://localhost:11434` (native Ollama or the existing Docker container).
- The configured model already available in Ollama. This project does not download models.

```powershell
cd revenueos-poc-accelerator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
curl.exe http://localhost:11434/api/tags
```

The current configured model is `qwen3:4b-instruct-2507-q4_K_M`. The HTTP client uses Python's standard library; `python-dotenv` loads settings from `.env` when the Streamlit app starts. Create a local configuration from the example:

```powershell
Copy-Item .env.example .env
# Edit .env only if your local Ollama URL/model/token settings differ.
streamlit run app.py
```

Alternatively, set the variables in PowerShell before launching. Environment variables already set in the shell take precedence over `.env` values.

`AGENT_MODE` defaults to `ollama`; `OLLAMA_BASE_URL` and `OLLAMA_MODEL` have the values above as defaults. Each request uses `stream: false`, JSON response format, and disables Qwen thinking mode. If a model emits literal newlines or tabs inside a JSON string, the parser narrowly escapes those control characters before normal JSON and schema validation; it does not skip validation or repair arbitrary malformed JSON. The agent generates the POC plan and Delivery handoff in separate calls with concise customer-specific context. `OLLAMA_NUM_PREDICT` defaults to 2,048 tokens; an incomplete generation gets at most one retry using `OLLAMA_RETRY_NUM_PREDICT` (3,072 by default). Both values must be positive, and the retry limit must be higher. Missing or invalid fields may get one focused repair. Responses stopped for length are never parsed as complete JSON. If handoff generation fails after the plan validates, the plan section is retained in the workflow event log and the overall request still reports failure. The HTTP client separates a 10-second TCP connection timeout from a 720-second inference/read timeout; total generation is capped at 780 seconds. Performance metrics are logged without logging prompt content. `.gitignore` excludes local `.env` files; do not commit credentials.


## Connect a CRM webhook

Install dependencies and start the ingestion API in a separate terminal:

```powershell
python -m pip install -r requirements.txt
uvicorn crm_ingestion:app --host 0.0.0.0 --port 8001
```

Set a strong `CRM_WEBHOOK_TOKEN` before starting the API and send it in the `X-RevenueOS-Token` header. The endpoint fails closed if the token is missing; anonymous access is available only when `CRM_WEBHOOK_ALLOW_ANONYMOUS=true` is explicitly set for local testing. Seed/demo events are excluded from outbound breach notifications; live CRM events are monitored. Example event:

```powershell
$env:CRM_WEBHOOK_TOKEN = "your-same-secret-from-.env"
$event = @{
  event_id = "crm-event-2026-0001"
  event_type = "opportunity_won"
  timestamp = "2026-11-02T09:30:00-05:00"
  source = "salesforce"
  owner = "Jordan Lee"
  account = "Example Community Bank"
  opportunity_id = "OPP-CRM-0001"
  region = "US"
  segment = "Community banking"
  regulator = "OCC"
  customer_brief = "Automate onboarding document review with analyst approval and auditability."
  systems = @("Core banking", "Document management")
  expected_timeline = "6 weeks"
  success_criteria = @("Reduce manual review time", "Maintain auditable approvals")
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/v1/events/opportunity-won `
  -Headers @{ "X-RevenueOS-Token" = $env:CRM_WEBHOOK_TOKEN } `
  -ContentType "application/json" -Body $event
```

A successful first delivery returns `created: true`; a replay with the same `event_id` returns `duplicate: true`. Check `GET http://localhost:8001/health` for API health. The webhook currently supports the normalized `opportunity_won` event contract; map the CRM's native payload to this contract in its webhook middleware/iPaaS. This POC does not include a vendor-specific Salesforce/HubSpot OAuth connector.

## Verify the local Ollama integration

In PowerShell, verify the API and model list:

```powershell
Invoke-RestMethod http://localhost:11434/api/tags
```

For the Docker setup, check `docker ps --filter name=jobpilot-ollama` and `docker logs --tail 50 jobpilot-ollama`. If the existing container is stopped, run `docker start jobpilot-ollama`. Confirm `qwen3:4b-instruct-2507-q4_K_M` appears in the API response and matches `OLLAMA_MODEL`. If you use a native Ollama install instead, its CLI commands are also available.

In the existing dashboard, select an opportunity, load its brief, and click **Generate POC plan**. The workflow retrieves relevant templates, passes the brief and selected template contents to Ollama, validates the response, and saves the plan, separate Delivery handoff, template references, generation metadata, and workflow events to SQLite. The dashboard identifies successful Ollama output by model name and keeps it as a draft for human review.

If connection, model, JSON, or validation fails, the dashboard displays the error and the workflow log records the failure. Connection-establishment timeout, inference-response timeout, and Ollama HTTP errors are reported separately. There is no silent offline substitution. Correct the service/model/configuration and submit the brief again to retry. Length-truncated calls get at most one higher-budget retry; a validated POC plan remains in the workflow event log if the separate handoff call fails.

To explicitly run the non-AI fixture for a UI demonstration, use a separate shell:

```powershell
$env:AGENT_MODE = "demo"
streamlit run app.py
```

The app labels these plans **Explicit demo fixture — not AI-generated**. Set `AGENT_MODE` back to `ollama` to use the model.

## Opportunity, review, and SLA behavior

On startup, the app seeds synthetic opportunity records from `data/briefs.json`; repeated initialization does not duplicate IDs. Seeded account, brief, owner, region, and event timestamp are checked against the selected opportunity. New CRM events can be posted to `POST /api/v1/events/opportunity-won`; they are persisted in the same SQLite database and immediately appear in the Streamlit queue. `event_id` is the idempotency key: replaying the same event returns the existing record, while reusing an opportunity ID with a different event ID is rejected. The API can be run independently from Streamlit; both processes start the same database-backed SLA monitor, and only one monitor is started per process/database path.

The pickup SLA starts from the original timezone-aware opportunity event timestamp, converted to US Eastern time (`America/New_York`). Weekends are excluded. Optional holidays can be configured with `SLA_HOLIDAYS` as comma-separated `YYYY-MM-DD` dates in that timezone. Green means before the one-business-day Amber threshold or picked up within the two-business-day SLA; Amber means at least one business day elapsed and the deadline has not passed; Red means the deadline passed without timely pickup. The monitor polls every `SLA_POLL_SECONDS` (default 60), independent of dashboard refresh. A durable opportunity-level breach trigger is created even if no POC plan exists yet; in that case the intake brief is attached as a pending-plan draft. If a late plan exists, the generated POC draft is attached. A unique event ID prevents duplicate escalations. Console notification is always emitted; setting `SLACK_WEBHOOK_URL` enables Slack delivery. Notification errors are recorded without deleting the trigger. Plan creation time is used as the pickup timestamp; review decisions remain separate human-governance events.

The US Solution Architect can accept, edit, or reject a draft. Decisions are timestamped and linked to plan/opportunity IDs; edits, reasons, template IDs, and retained template IDs are persisted. Editing alone is not acceptance: accepted reuse metrics and positive ranking feedback require an explicit Accept action. Rejections provide negative ranking feedback. Only each plan's latest decision is used for metrics/ranking.

Template ranking adds `+0.05` for a template retained in a latest accepted plan and `-0.05` for each candidate in a latest rejected plan, summed across plans and clamped to `+/-0.20`. Edits alone do not add positive or negative feedback. This is a transparent score adjustment, not a trained model.

## Tests

Run all unit tests; Ollama HTTP calls are mocked:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app.py agent.py retrieval.py workflow.py database.py sla_monitor.py crm_ingestion.py monitor_worker.py tests
```

To make a real local integration call, run the app with `AGENT_MODE=ollama` and generate a plan from the dashboard. The unit test suite does not require the service or make real model calls.

## Troubleshooting

- **Connection refused:** verify the Ollama Docker container is running (`docker ps --filter name=jobpilot-ollama`) or the native service is running, and that `OLLAMA_BASE_URL` points to the host/port reachable from the Python process.
- **Model not found:** run `ollama list` and set `OLLAMA_MODEL` to an installed tag. Do not rely on the app to download it.
- **Connection timeout:** TCP connection establishment is bounded at 10 seconds and reported as a connection failure.
- **Inference timeout:** once connected, response generation has a 720-second deadline, with a 780-second total plan deadline including any repair. It is reported as an inference timeout, not a connection failure. Check Ollama responsiveness and its logs.
- **Output token limit:** each stage gets one bounded retry at the higher `OLLAMA_RETRY_NUM_PREDICT` limit. A response with `done_reason=length` is never parsed or saved as complete; a validated POC plan is retained in workflow events if only the handoff stage fails.
- **Invalid JSON:** raw newline/tab characters inside JSON strings are escaped safely before parsing. Other malformed JSON, invalid sections, template references, or unsupported brief quotes are not silently accepted; the agent performs at most one focused repair call for schema validation errors. If validation still fails, the workflow reports the error and does not save a plan; review the brief and generated-output requirements before retrying.
- **The same validation error remains after updating code:** stop the Streamlit process with `Ctrl+C` in its terminal, confirm the terminal is in `revenueos-poc-accelerator`, and run `streamlit run app.py` again so the updated agent module is loaded.
- **Demo output shown:** check `AGENT_MODE`; the dashboard explicitly labels demo mode as non-AI.

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
