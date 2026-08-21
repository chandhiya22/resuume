"""
analyzer-api
Public-facing service. Accepts a resume (PDF) + job description,
extracts text, calls an LLM to score the match and generate feedback,
stores the resume in Blob, writes the result via Dapr state,
invokes audit-logger directly via Dapr, and publishes an event via Dapr pub/sub.
"""
import os
import uuid
import json
import httpx
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from azure.storage.blob import BlobServiceClient, ContentSettings
import pypdf
import io

app = FastAPI(title="ResumeIQ - Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before real use
    allow_methods=["*"],
    allow_headers=["*"],
)

DAPR_HTTP_PORT = os.environ.get("DAPR_HTTP_PORT", "3500")
STATE_STORE_NAME = os.environ.get("DAPR_STATE_STORE", "statestore")
PUBSUB_NAME = os.environ.get("DAPR_PUBSUB", "pubsub")
PUBSUB_TOPIC = os.environ.get("DAPR_PUBSUB_TOPIC", "analysis-events")

STORAGE_ACCOUNT_URL = os.environ.get("STORAGE_ACCOUNT_URL")  # e.g. https://stresumeiq01.blob.core.windows.net
CONTAINER_NAME = os.environ.get("BLOB_CONTAINER", "resumes")
KEY_VAULT_URL = os.environ.get("KEY_VAULT_URL")  # e.g. https://kv-resumeiq.vault.azure.net


def get_blob_service_client():
    from azure.identity import DefaultAzureCredential
    return BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=DefaultAzureCredential())


def get_llm_key() -> str:
    if not KEY_VAULT_URL:
        return os.environ.get("LLM_API_KEY_LOCAL", "")
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    client = SecretClient(vault_url=KEY_VAULT_URL, credential=DefaultAzureCredential())
    return client.get_secret("LlmApiKey").value


def extract_pdf_text(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text[:8000]  # cap to keep prompt reasonable


async def call_llm(resume_text: str, job_description: str, api_key: str) -> dict:
    """Calls OpenAI-compatible chat completion endpoint. Swap URL/model as needed."""
    prompt = f"""You are a resume screener. Compare this resume to the job description.
Return ONLY valid JSON with keys: "score" (0-100 integer), "strengths" (list of 3 short strings),
"gaps" (list of 3 short strings), "summary" (one sentence).

JOB DESCRIPTION:
{job_description}

RESUME:
{resume_text}
"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(content)


@app.get("/health")
def health():
    return {"status": "ok", "service": "analyzer-api"}


@app.post("/analyze")
async def analyze(job_description: str = Form(...), resume: UploadFile = None):
    analysis_id = str(uuid.uuid4())
    resume_bytes = await resume.read()
    resume_text = extract_pdf_text(resume_bytes)

    # 1. Upload original resume to Blob Storage
    if STORAGE_ACCOUNT_URL:
        blob_name = f"{analysis_id}-{resume.filename}"
        blob_client = get_blob_service_client().get_blob_client(container=CONTAINER_NAME, blob=blob_name)
        blob_client.upload_blob(
            resume_bytes, overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )

    # 2. Call LLM for scoring + feedback
    api_key = get_llm_key()
    result = await call_llm(resume_text, job_description, api_key)
    result["analysis_id"] = analysis_id

    # 3. Write result via Dapr state management (backed by Blob Storage)
    async with httpx.AsyncClient() as client:
        state_url = f"http://localhost:{DAPR_HTTP_PORT}/v1.0/state/{STATE_STORE_NAME}"
        await client.post(state_url, json=[{"key": analysis_id, "value": result}], timeout=10.0)

        # 4. Dapr INVOKE — synchronous call directly to audit-logger
        invoke_url = f"http://localhost:{DAPR_HTTP_PORT}/v1.0/invoke/audit-logger/method/log"
        try:
            await client.post(invoke_url, json={"analysis_id": analysis_id, "score": result.get("score")}, timeout=10.0)
        except httpx.HTTPError as e:
            print(f"audit-logger invoke failed: {e}")

        # 5. Dapr PUBLISH — async event, audit-logger also subscribes independently
        publish_url = f"http://localhost:{DAPR_HTTP_PORT}/v1.0/publish/{PUBSUB_NAME}/{PUBSUB_TOPIC}"
        try:
            await client.post(publish_url, json={"analysis_id": analysis_id, "score": result.get("score"), "event": "analysis_complete"}, timeout=10.0)
        except httpx.HTTPError as e:
            print(f"publish failed: {e}")

    return result


@app.get("/analysis/{analysis_id}")
async def get_analysis(analysis_id: str):
    """Fetch a past result back out of Dapr state."""
    async with httpx.AsyncClient() as client:
        state_url = f"http://localhost:{DAPR_HTTP_PORT}/v1.0/state/{STATE_STORE_NAME}/{analysis_id}"
        resp = await client.get(state_url, timeout=10.0)
        if resp.status_code == 204:
            return {"error": "not found"}
        return resp.json()
