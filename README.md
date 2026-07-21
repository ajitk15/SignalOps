# SignalOps — incident intelligence pipeline

SignalOps monitors middleware and application platforms with low AI cost.
Collection, threshold checks, trend detection, correlation, and duplicate
suppression are ordinary Python code. AI is reserved for new incidents that
need investigation.

Collection is plug-and-play: a source in `config/watchlist.yaml` names a
collector kind (`mq_mcp`, `http_json`, `prometheus`, or your own class in
`collectors/`), detection rules live in `config/rules.yaml` plus custom rules
managed from the dashboard's Rules tab (with templates for IBM MQ, IBM ACE,
Apigee, DataPower and Java/JVM), and the Agents tab shows the full workflow —
Collection → AI gate → Diagnostician → Report writer — whether or not AI is
enabled. ServiceNow receives both **incidents** (one ticket per incident) and
**approved KB articles** (created in Knowledge, updated when edited, retired
when deleted) — running as a dry-run outbox by default, with
`SERVICENOW_MODE=live` to write for real. Splunk, Dynatrace and ServiceNow
status live in the Integrations tab, with credentials configured via
environment variables only.

## Processing flow

1. The read-only MQ/ACE MCP collector retrieves current queue and channel data.
2. Splunk and Dynatrace readers can add focused historical context.
3. Deterministic rules detect queue depth, DLQ, channel, ACE flow, error-count,
   and rising-trend conditions.
4. Related symptoms are grouped by environment and service. Repeated findings
   are suppressed for the configured deduplication window.
5. Approved KB articles are searched first. A strong known match needs no AI.
6. A new unmatched issue can invoke exactly two agents: Diagnostician and
   Report Writer.
7. The incident is stored in SQLite and exposed through the API/dashboard.
8. After human-confirmed resolution, the system produces a KB draft for review.

The original AI-per-poll Watcher remains in the repository as a learning path,
but is disabled by default. Never enable it for an enterprise watchlist.

## Safety and cost controls

- MQ/ACE tools are read-only; the project makes no MQ or ACE changes.
- Routine collection makes zero model calls.
- AI is disabled by default (`ENABLE_INCIDENT_AI=false`).
- The legacy Watcher is disabled by default.
- Duplicate alerts do not repeatedly invoke AI.
- Strong approved-KB matches bypass AI.
- Unconfirmed AI output is never automatically published as knowledge.
- Splunk and Dynatrace access is intended to use read-only tokens.

## Setup

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit the copied `.env` with local credentials and keep it uncommitted. The
sample file uses zero-AI defaults. The server and one-shot collector load this
project-local file automatically; existing process environment values win.

The MCP collector defaults to the server at port 8010. It first reads explicit
`MQ_MCP_*` environment variables and, for this local learning environment, can
fall back to the existing server's `.env`. Secrets are never logged or copied
into this repository.

Explicit variables, when required:

```powershell
$env:MQ_MCP_URL='https://127.0.0.1:8010/mcp'
$env:MQ_MCP_AUTH_USER='<user>'
$env:MQ_MCP_AUTH_PASSWORD='<password>'
$env:MQ_MCP_TLS_CERT='C:\path\to\cert.pem'
```

Configure non-production MQ objects in `config/watchlist.yaml`. For enterprise
correlation, give each object an explicit `qmgr`, `environment`, and `service`.

## Zero-cost verification

```powershell
python -m unittest discover -s tests -v
python simulation\run_scenarios.py
python collect_mq_ace.py
```

The first command currently runs five tests. The last command performs one
read-only MCP collection cycle with AI disabled.

## Run continuously

```powershell
$env:ENABLE_MQ_ACE_COLLECTOR='true'
$env:ENABLE_INCIDENT_AI='false'
uvicorn server.app:app --port 8000
```

Open `http://localhost:8000`. To test AI investigation deliberately, restart
with `ENABLE_INCIDENT_AI=true` after configuring the model/API credentials.
The legacy demo loop is only enabled by `ENABLE_LEGACY_AI_WATCHER=true`.

## API

- `POST /api/observations` — ingest normalized MCP/monitoring observations.
- `GET /api/incidents` — list incidents.
- `GET /api/incidents/{id}` — incident evidence and report.
- `GET /api/incidents/{id}/kb-draft` — create a draft requiring human review.
- `WS /ws/events` — live dashboard events.

## Splunk and Dynatrace

The readers are implemented but disabled until read-only credentials are set:

```powershell
$env:SPLUNK_BASE_URL='https://splunk-test.example'
$env:SPLUNK_TOKEN='<read-only-token>'
$env:DYNATRACE_BASE_URL='https://tenant.live.dynatrace.com'
$env:DYNATRACE_TOKEN='<problems-read-token>'
```

They are queried only after a new rule finding, using a focused historical
window. A source failure is recorded in incident context and does not stop the
other sources or incident creation.

## Current implementation status

Implemented:

- Live TLS/Basic-Auth connection to the MQ/ACE MCP server on port 8010.
- Batched queue inspection and per-queue-manager depth observations.
- Channel inspection support.
- Rule engine, correlation, deduplication, KB lookup, AI cost gate.
- Splunk/Dynatrace readers, incident persistence, APIs, simulation, tests.

Next:

- Add ACE integration-server/application/flow status mapping from the MCP
  response into normalized observations.
- Populate explicit enterprise service mappings in the watchlist.
- Validate Splunk/Dynatrace searches against test tenants.
- Connect the chosen ticket platform and add an approved KB publication step.
- Run high-volume and failure-recovery tests.

See `FEATURE_TESTING.md` for exact feature-by-feature commands,
`MANUAL_TESTING.md` for the shorter manual plan, and `plan.md` for phased
delivery status.

## Main files

```text
integrations/mq_ace_mcp.py  deterministic live MQ/ACE MCP client
integrations/context.py     Splunk and Dynatrace historical readers
detection.py                rules and deduplication/correlation
enterprise_pipeline.py      cost gate and incident routing
collect_mq_ace.py           one-shot and continuous collection
knowledge/service.py        approved-KB matching and KB drafts
store.py                    SQLite incident persistence
server/app.py               API, dashboard, and background collector
simulation/run_scenarios.py zero-cost scenarios
tests/                      automated tests
```
