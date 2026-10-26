# Build log

- Created a Streamlit brief intake and generated-plan view for the Brief-to-POC workflow.
- Added SQLite persistence for submitted briefs and generated plans, including a four-hour response SLA.
- Added deterministic keyword-overlap retrieval over sample briefs and playbook templates.
- Added a structured deterministic plan generator for use without credentials, plus an optional OpenAI-compatible chat-completions integration.
- Added core unit tests for retrieval, fallback generation, persistence, and SLA states.

## Run locally

From this directory, install `requirements.txt` and run `streamlit run app.py`. The application works without an API key. To enable LLM generation, configure `OPENAI_API_KEY` in the process environment; `OPENAI_MODEL` and `OPENAI_BASE_URL` are optional.
