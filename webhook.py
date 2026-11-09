import hmac
import os
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from database import Database
from integrations.tilda import is_paid_tilda_payload, normalize_tilda_order


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
database = Database(Path(os.getenv("CRM_DB_PATH") or str(PROJECT_DIR / "crm.sqlite3")))
database.initialize()
app = FastAPI(title="Shop CRM CRM Webhooks")


async def request_payload(request: Request) -> Dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    body = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[-1] if len(values) == 1 else values for key, values in parse_qs(body).items()}


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/tilda/{secret}")
async def tilda_webhook(secret: str, request: Request) -> Dict[str, Any]:
    configured_secret = os.getenv("TILDA_WEBHOOK_SECRET", "")
    if not configured_secret or not hmac.compare_digest(secret, configured_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request_payload(request)
    if payload.get("test") == "test":
        return {"ok": True, "test": True}
    if not is_paid_tilda_payload(payload):
        return {"ok": True, "ignored": "not_paid"}
    order, items = normalize_tilda_order(payload)
    if not order["order_number"]:
        raise HTTPException(status_code=422, detail="Order number is missing")
    order_id = database.upsert_order(order, items)
    return {"ok": True, "order_id": order_id}
