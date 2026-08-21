"""
audit-logger
Internal service. Receives:
  1. A direct Dapr INVOKE call from analyzer-api (POST /log) right after analysis.
  2. Independently, a Dapr pub/sub event on "analysis-events" (POST /events/analysis).
Both are logged; in a real system you'd send an email/Slack message here instead.
Keeps a small in-memory list you can query, for demo purposes.
"""
import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request

app = FastAPI(title="ResumeIQ - Audit Logger")

PUBSUB_NAME = os.environ.get("DAPR_PUBSUB", "pubsub")
TOPIC = os.environ.get("DAPR_PUBSUB_TOPIC", "analysis-events")

invoke_log = []   # entries received via direct Dapr invoke
event_log = []    # entries received via Dapr pub/sub


@app.get("/health")
def health():
    return {"status": "ok", "service": "audit-logger"}


@app.post("/log")
async def log_invoke(request: Request):
    """Called directly via Dapr service invocation from analyzer-api."""
    data = await request.json()
    entry = {**data, "received_at": datetime.now(timezone.utc).isoformat(), "via": "invoke"}
    invoke_log.append(entry)
    print(f"[INVOKE] analysis {data.get('analysis_id')} scored {data.get('score')}")
    return {"status": "logged"}


@app.get("/dapr/subscribe")
def subscribe():
    return [{"pubsubname": PUBSUB_NAME, "topic": TOPIC, "route": "/events/analysis"}]


@app.post("/events/analysis")
async def handle_event(request: Request):
    """Called by Dapr when a message arrives on the analysis-events topic."""
    event = await request.json()
    data = event.get("data", event)
    entry = {**data, "received_at": datetime.now(timezone.utc).isoformat(), "via": "pubsub"}
    event_log.append(entry)
    print(f"[EVENT] analysis {data.get('analysis_id')} complete, score {data.get('score')}")
    return {"status": "SUCCESS"}


@app.get("/recent")
def recent():
    return {
        "invoke_count": len(invoke_log),
        "event_count": len(event_log),
        "invoke_log": list(reversed(invoke_log))[:20],
        "event_log": list(reversed(event_log))[:20],
    }
