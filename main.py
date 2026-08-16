
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
import hmac
import hashlib
import uuid
import datetime
import logging
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Depends, Header, status
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    init_db, get_db, Event, Comment, Rule, DMAttempt,
    increment_stats_counter, get_stats_counter, AsyncSessionLocal, utcnow
)
import worker

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("linkplease.main")

API_KEY = os.getenv("API_KEY", "")
worker.set_api_key(API_KEY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for starting database and background workers."""
    logger.info("Initializing database...")
    await init_db()

    # Recover any unmatched comments on boot
    await worker.recover_unmatched_comments()

    # Start background worker coroutines
    matcher_task = asyncio.create_task(worker.matcher_worker())
    sender_task = asyncio.create_task(worker.sender_worker())
    poller_task = asyncio.create_task(worker.poller_worker())

    logger.info("Application startup complete.")
    yield

    # Clean up background tasks on shutdown
    matcher_task.cancel()
    sender_task.cancel()
    poller_task.cancel()
    logger.info("Application shutdown complete.")


app = FastAPI(
    title="LinkPlease Instagram DM Engine",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware for local frontend dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic Schemas
class RuleCreate(BaseModel):
    keyword: str
    dm_message: str


class RuleResponse(BaseModel):
    rule_id: str
    keyword: str
    dm_message: str


class StatsResponse(BaseModel):
    sent: int
    failed: int
    queued: int
    duplicates_blocked: int


class KeyConfigReq(BaseModel):
    api_key: str


# Helper: HMAC SHA256 Verification
def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verifies X-PseudoGram-Signature HMAC-SHA256 header."""
    if not API_KEY:
        # If API key is not configured, signature check is disabled in dev mode
        return True
    if not signature_header:
        return False

    prefix = "sha256="
    if signature_header.startswith(prefix):
        provided_hash = signature_header[len(prefix):]
    else:
        provided_hash = signature_header

    expected_hash = hmac.new(
        API_KEY.encode('utf-8'),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_hash, provided_hash)


# Required API Contract Endpoint 1: POST /webhook
@app.post("/webhook", status_code=200)
async def handle_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_pseudogram_signature: Optional[str] = Header(None, alias="X-PseudoGram-Signature")
):
    raw_body = await request.body()

    # Part B Requirement: Signature verification
    if API_KEY and not verify_signature(raw_body, x_pseudogram_signature):
        logger.warning(f"Invalid webhook signature header: {x_pseudogram_signature}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    data = payload.get("data", {})

    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Missing required event_id or event_type")

    comment_id = data.get("comment_id")

    # 1. Deduplicate event_id (Stream redelivers ~8% events)
    event_record = Event(
        event_id=event_id,
        event_type=event_type,
        comment_id=comment_id,
        received_at=utcnow()
    )
    db.add(event_record)
    try:
        await db.commit()
    except IntegrityError:
        # Duplicate event_id received! Return 200 immediately & increment duplicates_blocked counter
        await db.rollback()
        await increment_stats_counter(db, "duplicates_blocked", 1)
        logger.info(f"Duplicate event_id received ({event_id}). Blocked.")
        return {"status": "ok", "detail": "duplicate_event_blocked"}

    # 2. Process event type
    if event_type == "comment.created":
        post_id = data.get("post_id")
        text = data.get("text", "")
        from_user = data.get("from", {})
        user_id = from_user.get("user_id", "")
        username = from_user.get("username", "")

        comment_obj = await db.scalar(select(Comment).where(Comment.comment_id == comment_id))
        if not comment_obj:
            comment_obj = Comment(
                comment_id=comment_id,
                post_id=post_id,
                text=text,
                user_id=user_id,
                username=username,
                created_at=utcnow()
            )
            db.add(comment_obj)
        else:
            comment_obj.text = text
            comment_obj.user_id = user_id
            comment_obj.username = username
        await db.commit()

        # Enqueue comment_id for matcher worker processing
        await worker.matcher_queue.put(comment_id)

    elif event_type == "comment.deleted":
        comment_obj = await db.scalar(select(Comment).where(Comment.comment_id == comment_id))
        if not comment_obj:
            comment_obj = Comment(
                comment_id=comment_id,
                text="",
                user_id="",
                deleted_at=utcnow()
            )
            db.add(comment_obj)
        else:
            comment_obj.deleted_at = utcnow()

        await db.commit()
        # Enqueue to matcher to handle tombstone / pending cancellation
        await worker.matcher_queue.put(comment_id)

    return {"status": "ok"}


# Required API Contract Endpoint 2: POST /rules
@app.post("/rules", response_model=RuleResponse, status_code=201)
async def create_rule(
    rule_in: RuleCreate,
    db: AsyncSession = Depends(get_db)
):
    rule_id = f"rule_{uuid.uuid4().hex[:10]}"
    rule = Rule(
        rule_id=rule_id,
        keyword=rule_in.keyword,
        dm_message=rule_in.dm_message,
        created_at=utcnow()
    )
    db.add(rule)
    await db.commit()
    return RuleResponse(
        rule_id=rule.rule_id,
        keyword=rule.keyword,
        dm_message=rule.dm_message
    )


@app.get("/rules", response_model=List[RuleResponse])
async def list_rules(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Rule))
    rules = res.scalars().all()
    return [
        RuleResponse(rule_id=r.rule_id, keyword=r.keyword, dm_message=r.dm_message)
        for r in rules
    ]


# Required API Contract Endpoint 3: GET /stats
@app.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)):
    # 1. Count sent (delivered)
    res_sent = await db.execute(select(func.count()).select_from(DMAttempt).where(DMAttempt.status == "delivered"))
    sent_count = res_sent.scalar_one()

    # 2. Count failed
    res_failed = await db.execute(select(func.count()).select_from(DMAttempt).where(DMAttempt.status == "failed"))
    failed_count = res_failed.scalar_one()

    # 3. Count queued (pending + queued)
    res_queued = await db.execute(select(func.count()).select_from(DMAttempt).where(DMAttempt.status.in_(["pending", "queued"])))
    queued_count = res_queued.scalar_one()

    # 4. Read duplicates_blocked atomic counter
    duplicates_blocked = await get_stats_counter(db, "duplicates_blocked")

    return StatsResponse(
        sent=sent_count,
        failed=failed_count,
        queued=queued_count,
        duplicates_blocked=duplicates_blocked
    )


# API Key Management Configuration Endpoints
@app.get("/api/config")
async def get_config():
    return {"api_key": API_KEY, "is_configured": bool(API_KEY)}


@app.post("/api/config")
async def set_config(config: KeyConfigReq):
    global API_KEY
    API_KEY = config.api_key
    worker.set_api_key(API_KEY)
    return {"status": "ok", "api_key": API_KEY}


# Mount Static Files Dashboard
if os.path.exists("static"):
    app.mount("/dashboard", StaticFiles(directory="static", html=True), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists("static/index.html"):
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>LinkPlease Instagram DM Automation API is running</h1><p>Visit <a href='/stats'>/stats</a> or <a href='/dashboard'>/dashboard</a></p>"

