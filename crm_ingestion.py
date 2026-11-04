"""CRM webhook API for RevenueOS opportunity-won events."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from database import initialize_database, ingest_opportunity_event
from monitor_worker import start_sla_monitor, stop_sla_monitor

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("REVENUEOS_DATABASE_PATH", BASE_DIR / "data" / "revenueos.db"))


class OpportunityWonEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = "opportunity_won"
    timestamp: str = Field(min_length=1)
    source: str = "crm_webhook"
    owner: str = Field(min_length=1)
    account: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    segment: Optional[str] = None
    regulator: Optional[str] = None
    customer_brief: str = Field(min_length=1)
    systems: List[str] = Field(default_factory=list)
    expected_timeline: Optional[str] = None
    success_criteria: List[str] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database(DATABASE_PATH, BASE_DIR / "data" / "briefs.json")
    start_sla_monitor(DATABASE_PATH)
    try:
        yield
    finally:
        stop_sla_monitor(DATABASE_PATH)


app = FastAPI(title="RevenueOS CRM Event Ingestion", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "revenueos-crm-ingestion"}


@app.post("/api/v1/events/opportunity-won", status_code=202)
def receive_opportunity_won(event: OpportunityWonEvent, x_revenueos_token: Optional[str] = Header(default=None)):
    expected_token = os.getenv("CRM_WEBHOOK_TOKEN", "").strip()
    allow_anonymous = os.getenv("CRM_WEBHOOK_ALLOW_ANONYMOUS", "false").lower() == "true"
    if not expected_token and not allow_anonymous:
        raise HTTPException(status_code=503, detail="CRM_WEBHOOK_TOKEN is not configured.")
    if expected_token and x_revenueos_token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid CRM webhook token.")
    try:
        result = ingest_opportunity_event(DATABASE_PATH, event.model_dump() if hasattr(event, "model_dump") else event.dict())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"status": "accepted", **result}
