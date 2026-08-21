# ResumeIQ — AI Resume Screener & Feedback Tool

Upload a resume + paste a job description, get back a match score and specific feedback. Built on Azure Container Apps + Dapr, using all three core Dapr building blocks: service invocation, state management, and pub/sub.

## Architecture

```
User uploads resume (PDF) + job description
        │
        ▼
  analyzer-api (ACA, public, Dapr-enabled)
        │
        ├── uploads resume ─────────────▶ Blob Storage
        ├── fetches LLM key ────────────▶ Key Vault
        ├── calls LLM (score + feedback)
        ├── writes result via Dapr state ▶ Blob Storage (dapr-state container)
        ├── Dapr INVOKE ────────────────▶ audit-logger  (sync log call)
        └── Dapr PUBLISH ───────────────▶ Service Bus topic "analysis-events"
                                                  │
                                                  ▼
                                     audit-logger (ACA, internal, Dapr-enabled)
                                     subscribes to "analysis-events"
                                     logs every completed analysis
```

## Repo structure

```
resumeiq/
├── analyzer-api/       # public-facing, does the actual analysis
├── audit-logger/       # internal, invoked directly AND subscribes to events
├── frontend/           # single HTML page: upload + results
└── dapr-components/    # statestore.yaml, pubsub.yaml (Azure-backed, reference only)
```

## Why both invoke AND pub/sub to the same service

This is intentional, to exercise all three Dapr building blocks:
- **Invoke**: `analyzer-api` calls `audit-logger`'s `/log` endpoint synchronously right after analysis completes — this is the direct service-to-service call.
- **Publish/Subscribe**: `analyzer-api` also publishes an `analysis-events` message; `audit-logger` independently subscribes to it. In a real system this decouples the logger from the analyzer entirely (you could remove the invoke call and rely purely on pub/sub), but here we use both so you can see and demo each mechanism working.

## Critical lesson from the last project — read before deploying

Dapr service invocation to a **scale-to-zero** container app can fail with "connection refused" if the target hasn't woken up yet. Before deploying to Azure:
1. Set **Min replicas = 1** on both `analyzer-api` and `audit-logger` from the start (Container App → Scale → Min replicas: 1) — don't leave this for later.
2. Enable Dapr **before** the first real traffic hits each app, and always force a **new revision** after changing Dapr settings (Revisions and replicas → Create new revision) — Dapr config changes don't apply to already-running revisions.
3. Verify each Dapr component's connection string with the **copy icon** on the source resource (Key Vault, Service Bus) — never manually select/copy text, which risks silently truncating the value.

## Local dev (Dapr CLI, Redis-backed)

```bash
dapr init  # one-time, needs Docker

mkdir -p dapr-components-local
cat > dapr-components-local/statestore.yaml << 'EOF'
componentType: state.redis
version: v1
metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""
EOF

cat > dapr-components-local/pubsub.yaml << 'EOF'
componentType: pubsub.redis
version: v1
metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""
EOF
```

Run each service with its own sidecar:
```bash
# Terminal 1
cd analyzer-api && dapr run --app-id analyzer-api --app-port 8000 --dapr-http-port 3500 \
  --resources-path ../dapr-components-local -- uvicorn app.main:app --port 8000

# Terminal 2
cd audit-logger && dapr run --app-id audit-logger --app-port 8001 --dapr-http-port 3501 \
  --resources-path ../dapr-components-local -- uvicorn app.main:app --port 8001
```

Test:
```bash
curl -X POST http://localhost:8000/analyze \
  -F "job_description=Looking for a Python backend engineer with AWS experience" \
  -F "resume=@/path/to/resume.pdf"
```

## Deploy checklist (Azure)

- [ ] Resource group `rg-resumeiq`
- [ ] Storage account + `resumes` container + `dapr-state` container
- [ ] Key Vault with `LlmApiKey` secret (OpenAI or Gemini key)
- [ ] ACR, both images built and pushed
- [ ] Service Bus namespace, topic `analysis-events`, subscription `audit-logger-sub`
- [ ] ACA environment
- [ ] Deploy `analyzer-api` (external ingress, port 8000) and `audit-logger` (internal ingress, port 8001) — **Min replicas = 1 on both, set before first deploy**
- [ ] Enable Dapr on both (App IDs: `analyzer-api`, `audit-logger`) — **before** granting traffic / testing
- [ ] Managed identity + RBAC: both apps → Key Vault Secrets User; analyzer-api → Storage Blob Data Contributor
- [ ] Register Dapr components: `statestore` (Blob-backed), `pubsub` (Service Bus-backed) — paste connection strings via copy icon, never manual select
- [ ] Force a fresh revision on both apps AFTER all Dapr settings are saved
- [ ] Test end to end, checking Log stream on both apps
