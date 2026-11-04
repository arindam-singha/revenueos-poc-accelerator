# Cut list and scaling limits

## Deliberately deferred

- Multi-user identity, tenant isolation, and role-based authorization. The CRM webhook uses a shared token, not per-user identity.
- Vendor-specific CRM connectors/OAuth and automatic mapping of native Salesforce/HubSpot payloads. The current endpoint accepts a normalized webhook contract.
- Embedding/vector retrieval, reranking services, and managed model hosting.
- External durable job infrastructure, notification retry/backoff, request streaming, and model-provider failover. SLA polling currently runs as an in-process daemon thread in the API and Streamlit processes; SQLite uniqueness prevents duplicate breach records.
- Model evaluation datasets, automated regression scoring, and production guardrail monitoring.
- Automatic holiday-calendar provider integration. A configurable `SLA_HOLIDAYS` list supports manually supplied US business-calendar holidays; weekends are excluded by default.
- Production deployment, backups, centralized telemetry, delivery guarantees for Slack/email notifications, and operational on-call routing. Console notification is always enabled; Slack Incoming Webhook is optional.

## Likely first failures at 10x volume

- Keyword-overlap retrieval will produce weaker rankings as the template/example corpus grows; query relevance will degrade before SQLite capacity is the bottleneck.
- Synchronous Ollama calls in Streamlit will tie up request handling under concurrent users and can exceed the current bounded timeout.
- The single local Ollama model may queue requests or exhaust available memory/compute.
- The single SQLite file has no connection pool, workload separation, or operational backup policy; write contention and manual recovery are likely first persistence issues.
- Plan/evidence JSON and workflow-event tables are not paginated or retention-managed at scale.
- Dashboard-wide queries have not been load-tested. The SLA monitor polls every 60 seconds by default and has not been load-tested across multiple application replicas.

## Explicitly not claimed

The ranking adjustment is a bounded, explainable arithmetic signal based on latest reviewer outcomes. It is not model training or an autonomous learning system. Unit tests mock Ollama and do not establish model quality, latency, or safety.
