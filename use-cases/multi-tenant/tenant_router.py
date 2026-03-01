"""
Multi-tenant email router — FastAPI + Commune.

Routes inbound emails to per-tenant AI agents based on which inbox received
the message. Each tenant gets an isolated inbox; the router dispatches to
the correct agent and sends replies scoped to that tenant.

Install:
    pip install fastapi uvicorn openai commune-mail

Usage:
    export COMMUNE_API_KEY=comm_...
    export OPENAI_API_KEY=sk-...
    export COMMUNE_WEBHOOK_SECRET=whsec_...
    uvicorn tenant_router:app --port 8080
"""

import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from openai import OpenAI

from commune import CommuneClient
from commune.webhooks import verify_signature, WebhookVerificationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

WEBHOOK_SECRET  = os.environ["COMMUNE_WEBHOOK_SECRET"]
COMMUNE_API_KEY = os.environ["COMMUNE_API_KEY"]

commune = CommuneClient(api_key=COMMUNE_API_KEY)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ---------------------------------------------------------------------------
# Tenant registry
# Each tenant has a dedicated inbox and a support persona for the LLM.
# ---------------------------------------------------------------------------

TENANTS: dict[str, dict] = {
    "inbox_acme_abc123": {
        "name": "Acme Corp",
        "persona": "You are a helpful support agent for Acme Corp, a B2B SaaS company.",
        "inbox_id": "inbox_acme_abc123",
        "escalation_email": "escalations@acme.com",
    },
    "inbox_globex_def456": {
        "name": "Globex Industries",
        "persona": "You are a friendly support agent for Globex Industries, an e-commerce platform.",
        "inbox_id": "inbox_globex_def456",
        "escalation_email": "support@globex.com",
    },
    "inbox_initech_ghi789": {
        "name": "Initech",
        "persona": "You are a professional support agent for Initech, an enterprise software company.",
        "inbox_id": "inbox_initech_ghi789",
        "escalation_email": "help@initech.com",
    },
}

import redis as _redis

_redis_conn = _redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
_IDEMPOTENCY_TTL = 86400  # 24 hours


def _is_duplicate_event(event_id: str) -> bool:
    """Atomically claim event_id across all workers and replicas via Redis SETNX."""
    key = f"commune:processed:{event_id}"
    return not _redis_conn.set(key, "1", nx=True, ex=_IDEMPOTENCY_TTL)


def get_tenant(inbox_id: str) -> Optional[dict]:
    """Look up tenant config by inbox ID."""
    return TENANTS.get(inbox_id)


def get_thread_context(thread_id: str, inbox_id: str) -> str:
    """
    Fetch recent messages from the thread for LLM context.

    Scoped to inbox_id so cross-tenant thread reads are rejected by the API
    (returns 404 if thread_id doesn't belong to inbox_id).
    """
    messages = commune.threads.messages(
        thread_id=thread_id,
        inbox_id=inbox_id,   # scope to this tenant's inbox
        order="asc",
    )
    if not messages:
        return ""
    parts = []
    for msg in messages[-5:]:  # last 5 messages for context
        sender = next(
            (p.identity for p in msg.participants if p.role == "sender"),
            "unknown",
        )
        parts.append(f"{sender}: {(msg.content or '')[:200]}")
    return "\n".join(parts)


def generate_reply(
    persona: str,
    thread_context: str,
    customer_message: str,
    subject: str,
) -> str:
    """Call OpenAI to generate a tenant-specific reply."""
    messages = [{"role": "system", "content": persona}]
    if thread_context:
        messages.append({
            "role": "system",
            "content": f"Previous conversation:\n{thread_context}",
        })
    messages.append({
        "role": "user",
        "content": f"Subject: {subject}\n\nCustomer message:\n{customer_message}",
    })
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
    )
    return completion.choices[0].message.content.strip()


@app.post("/webhooks/commune")
async def handle_webhook(
    request: Request,
    x_commune_signature: str = Header(default=""),
    x_commune_timestamp: str = Header(default=""),
):
    """
    Multi-tenant webhook endpoint.

    Routes inbound emails to the correct tenant agent and sends a reply
    scoped to that tenant's inbox.
    """
    raw_body: bytes = await request.body()

    try:
        verify_signature(
            payload=raw_body,
            signature=x_commune_signature,
            secret=WEBHOOK_SECRET,
            timestamp=x_commune_timestamp,
        )
    except WebhookVerificationError:
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)
    event_id = payload.get("event_id", "")

    if _is_duplicate_event(event_id):
        logger.info(f"Duplicate event {event_id} — skipping")
        return {"status": "duplicate"}

    if payload.get("event") != "message.received":
        return {"status": "ignored"}

    data      = payload.get("data", {})
    message   = data.get("message", {})
    inbox_id  = data.get("inbox_id", "")
    thread_id = data.get("thread_id", "")
    subject   = data.get("subject", "(no subject)")
    sender    = message.get("from", "")
    body_text = data.get("text", "")

    if not sender or not body_text:
        return {"status": "skipped"}

    # Route to tenant
    tenant = get_tenant(inbox_id)
    if not tenant:
        logger.warning(f"Unknown inbox_id {inbox_id} — no tenant found")
        return {"status": "unrouted"}

    logger.info(
        f"Routing message from {sender} to tenant '{tenant['name']}' "
        f"(thread={thread_id})"
    )

    thread_context = get_thread_context(thread_id, inbox_id)

    reply_text = generate_reply(
        persona=tenant["persona"],
        thread_context=thread_context,
        customer_message=body_text,
        subject=subject,
    )

    commune.messages.send(
        to=sender,
        subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
        text=reply_text,
        inbox_id=tenant["inbox_id"],
        thread_id=thread_id,   # continues the customer's existing thread
    )

    logger.info(f"Reply sent to {sender} for tenant '{tenant['name']}'")
    return {"status": "ok", "tenant": tenant["name"]}


@app.get("/health")
async def health():
    return {"status": "ok"}
