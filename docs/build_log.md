# Build log

## Implementation record

- The application uses the existing Streamlit, SQLite, keyword-overlap retrieval, and urllib standard-library HTTP stack.
- The configured local agent is Ollama Chat API (`POST /api/chat`), default model `qwen3:4b-instruct-2507-q4_K_M`; this is configuration supplied for the project, not a claim that the model was called during development.
- `AGENT_MODE=ollama` is the production path. `AGENT_MODE=demo` is a visibly labelled, deterministic fixture and is never substituted after model failure.
- The agent receives the brief and a ranked subset of actual library templates, requests JSON, validates structure/template references, and permits one bounded repair request.
- SQLite stores generation success/failure, review events, plan content, generation metadata, and template references.

## AI tools and model history

- Coding assistant: Copilot SDK in VS Code. The specific underlying coding model was not recorded in the source project; do not infer or claim one.
- POC-planning model configured by the project: `qwen3:4b-instruct-2507-q4_K_M` served locally by Ollama.
- Ollama `/api/tags` confirmed the local service and configured model were available. Two short direct chat calls completed, taking about 19 and 43 seconds on this host; these confirmed connectivity only, not plan quality.
- Full workflow calls with 120-second and 240-second client timeouts did not finish in time. A later call with a 600-second timeout completed an initial response and one repair response, but both failed plan validation because the returned plan was invalid JSON (`Expecting value` near line 102). No plan was saved.
- A compacted-prompt retry with a 900-second per-call timeout also failed validation with malformed JSON near character 7,042. The endpoint response did not record its completion reason in the application log, so token-limit truncation is plausible but unconfirmed.
- The request budget was raised to 2,400 tokens. The next real workflow call returned Ollama's explicit `done_reason="length"` before plan validation; this confirms the response was capped, not merely malformed. The client now reports that condition without spending time on a repair call.
- The prompt was further constrained to concise phrases of at most 10 words per string. A subsequent live workflow call completed generation and the single repair request, but validation still failed because a generated `facts_from_brief` item did not cite a verbatim brief excerpt. No plan was saved. End-to-end real-model generation is not verified.
- Latest hardening: the full JSON shape explicitly describes every handoff array, including non-empty `expected_workflow`; concise instructions avoid repeating constraints. Missing workflow steps derive from generated phases/tasks or next steps; other field repairs request and merge only the invalid path. Malformed/truncated JSON fails immediately, avoiding a second full generation. Ollama calls log elapsed time, completion status, prompt byte size, output limit, and validation diagnostics; briefs are excluded from logs.
- Follow-up investigation found this prior 3,072-token/300-second note was stale: the active code was using `urlopen(timeout=300)` for a non-streaming request. In urllib, that socket timeout also applies while waiting to read the response body. The UI request blocks through the agent and workflow; socket timeout was reported with connection errors. A failure at five minutes is consistent with the application read timeout and subsequent Ollama task cancellation, but without correlated client/server timestamps it cannot rule out an independent Ollama-side deadline.
- The client now opens a direct HTTP(S) connection with separate 10-second connect, 720-second inference/read, and 780-second total plan deadlines; output is capped at 2,048 tokens. Truncated and malformed responses do not trigger another full-plan call. A missing field gets one focused repair within the remaining plan deadline.
- The complete initial request payload for the first synthetic brief measured 4,882 bytes, including all 27 top-level plan fields and 22 handoff fields (one retrieved template). Prompt size is logged as a byte count only; prompt/brief content is not logged.

## Prompt and validation iterations

- Initial implementation requested a structured plan and handoff in one JSON response.
- Validation was tightened to check required sections, phases, measurable evaluation criteria, matching opportunity identity, template IDs, source regions, and reusable components.
- A single repair request returns validation diagnostics to the same model. A second invalid response remains a visible failure.

## Failures and discarded approaches

- The former optional OpenAI-compatible provider and implicit deterministic fallback did not satisfy the local Ollama requirement. App generation now defaults to Ollama; only explicit demo mode uses the fixture.
- Record observed local service/model errors and actual prompt outcomes here after manual testing. No model performance or response quality should be claimed from mocked tests.

## Verification record

- `python -m unittest discover -s tests -v`: 35 tests passed after the transport and timeout changes; Ollama responses are mocked.
- `python -m compileall -q app.py agent.py retrieval.py workflow.py database.py sla_monitor.py tests`: passed after the latest changes.
- Streamlit `AppTest` smoke test rendered the dashboard without exceptions; existing `use_container_width` deprecation warnings remain.
- Real end-to-end Ollama plan generation has not been rerun with the latest changes. The previous implementation's last real call failed validation after its bounded repair; the revised flow is verified with mocked responses only.


## Follow-up repair after ZIP review (2026-10-28)

- Reproduced the parser failure with a JSON string containing a literal newline. Added a narrow parser recovery that escapes raw JSON control characters only inside quoted strings, then runs ordinary JSON parsing and the existing schema validation. Structural whitespace and already-escaped characters are preserved; arbitrary malformed JSON is still rejected.
- Added regression tests for raw newline recovery and ordinary multiline JSON formatting.
- The app now loads the local `.env` before importing modules that read configuration, and `python-dotenv` is listed as a dependency. `.env` itself must remain local and is not included in the distributable ZIP.
- Corrected a time-dependent test assertion: an SLA deadline is anchored to the original opportunity event and can legitimately precede a plan created after a breach. The test now compares the deadline to the original event timestamp.
- Verification in the repair environment: `python -m compileall -q ...` passed; `python -m pytest -q` passed 41 tests. A Streamlit server smoke test could not run in this environment because Streamlit is not installed here; install `requirements.txt` before running the UI. No live Ollama generation was available in this repair environment, so end-to-end model output still needs one local verification after applying the ZIP.

## CRM ingestion and SLA monitoring extension (2026-11-02)

- Added `POST /api/v1/events/opportunity-won` in `crm_ingestion.py` using FastAPI/Pydantic. The normalized webhook contract includes CRM event ID/type, source timestamp, owner, account, opportunity ID, region, segment, regulator, customer brief, systems, expected timeline, and success criteria.
- Added event-ID idempotency and unique-opportunity protection. Replayed event IDs return the existing event; an event ID reused for another opportunity or a new event ID for an existing opportunity is rejected.
- Extended SQLite with additive schema upgrades for CRM metadata and a durable opportunity-level SLA trigger table. Existing database rows and existing plan/review workflows remain supported.
- Added a background monitor that polls independently of Streamlit reruns, calculates the two-business-day SLA in US Eastern time, excludes weekends and configured `SLA_HOLIDAYS`, and writes one escalation per breached live CRM event. The breach context includes the source brief and generated POC draft when one exists; otherwise an explicit `poc_plan_not_started` intake-brief draft is attached.
- Added console escalation and optional Slack Incoming Webhook delivery. Notification errors are recorded on the trigger; this POC does not yet provide durable notification retry/backoff or vendor-specific CRM authentication/OAuth.
- Updated the dashboard queue with new CRM intake counts, source, and intake state; added an opportunity-level escalation view. Demo seed records are excluded from external escalation delivery.
- Added webhook, duplicate-delivery, timestamp validation, opportunity collision, and SLA breach tests. Verification: `python -m compileall -q .` passed and `python -m unittest discover -s tests -v` passed 50 tests. Tests use a temporary SQLite database and mocked/local test clients; no live CRM, Slack, or Ollama integration was exercised by this test run.
