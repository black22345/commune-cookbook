# ADR-008: Background Processing for Webhook Handlers

**Status:** Accepted
**Date:** 2026-03-01
**Technical Area:** Reliability / Performance

---

## Context

Commune requires webhook handlers to acknowledge delivery by returning an HTTP 2xx response within **30 seconds**. If the handler does not respond within this window, Commune marks the delivery attempt as failed and schedules a retry with exponential backoff (approximately 5s, 30s, 5m, 30m, 2h). After a configurable number of failed attempts, the event is moved to a dead-letter queue or dropped.

The 30-second constraint exists in tension with what email agents typically need to do in response to an inbound email:

| Operation | Typical latency |
|---|---|
| LLM completion (GPT-4o, Claude Sonnet) | 3–25s depending on output length |
| LLM completion with tool use (multi-step) | 10–60s |
| Database write (RDS, Firestore) | 5–50ms |
| Outbound `messages.send()` | 200–800ms |
| Attachment download + processing | 1–30s |
| Multi-agent orchestration (sequential LLM calls) | 30–120s |

For simple, fast agents — a single LLM call that generates a short reply — synchronous processing within the webhook handler is feasible. A handler that consistently completes in under 10 seconds has comfortable headroom within the 30-second window.

For agents that make multiple LLM calls, use tools, write to databases, or orchestrate across multiple services, synchronous processing within the handler is structurally unsafe. LLM APIs experience latency spikes during peak load, and an agent that completes in 8 seconds under normal conditions may take 45 seconds during an OpenAI degradation event. Under load, this is not hypothetical — LLM API P99 latencies regularly exceed 30 seconds for large outputs.

**The failure cascade from synchronous timeout:**

1. Handler exceeds 30 seconds; Commune marks delivery as failed
2. Commune redelivers the webhook (at-least-once semantics)
3. The handler runs again — and may be running concurrently with the original handler if it hasn't exited yet
4. Both handler instances call `messages.send()` — without idempotency keys, the user receives two emails (ADR-006)
5. Both handler instances attempt database writes — may cause constraint violations or duplicate records
6. Under sustained load, all available handler threads are blocked on long-running operations, causing new webhooks to queue and eventually time out on delivery

**The secondary concern: resource consumption under concurrent load.**

If the webhook handler blocks on LLM API calls inline, each concurrent inbound email consumes one thread (or one coroutine, in async frameworks) for the duration of the LLM call. For a Flask application with a default WSGI server (Gunicorn, typically 4–8 workers), 4–8 concurrent inbound emails is enough to fully saturate all worker capacity. Additional webhooks queue in Commune's retry system. As queued events grow, redelivery intervals lengthen, and the effective response time for email agents degrades from seconds to minutes under modest load.

Background processing decouples webhook acknowledgement from webhook processing: the handler returns 200 immediately after validating the signature and enqueueing the payload. The actual processing — LLM calls, database writes, outbound sends — occurs in a separate worker process with no time constraint and independent horizontal scalability.

---

## Decision

Webhook handlers validate the signature, acknowledge delivery by returning 200, and enqueue the processing payload to a work queue. No LLM calls, database writes, or outbound `messages.send()` calls occur within the webhook handler itself.

**Architecture:**

```
[Commune backend]
      |
      | POST /webhook
      v
[Webhook handler]
  1. Verify HMAC signature        (microseconds)
  2. Extract and validate payload (microseconds)
  3. Enqueue to work queue        (1-10ms)
  4. Return HTTP 200              (total: <50ms)
      |
      | (async)
      v
[Background worker]
  5. Dequeue payload
  6. Prompt injection check       (ADR-007)
  7. LLM call(s)                  (3-60s, no constraint)
  8. Database write               (5-50ms, no constraint)
  9. messages.send() with         (200-800ms, no constraint)
     idempotency_key              (ADR-006)
```

**Flask + Celery implementation:**

```python
# webhook.py
from flask import Flask, request, jsonify
from commune import CommuneClient
from tasks import process_inbound_email
import os

app = Flask(__name__)
client = CommuneClient(api_key=os.environ["COMMUNE_API_KEY"])

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    raw_body = request.get_data()

    try:
        payload = client.webhooks.verify(raw_body, request.headers)
    except Exception:
        return jsonify({"error": "Invalid signature"}), 401

    # Enqueue — return immediately
    # Pass all required context: the worker cannot retrieve it from the webhook later
    process_inbound_email.delay(
        message_id=payload.message.id,
        inbox_id=payload.message.inbox_id,
        thread_id=payload.message.thread_id,   # Must be passed; cannot retrieve later (ADR-001)
        sender_email=payload.message.from_email,
        subject=payload.message.subject,
        body_text=payload.message.body_text,
        metadata=payload.message.metadata,
    )

    return jsonify({"status": "queued"}), 200
```

```python
# tasks.py
from celery import Celery
from commune import CommuneClient
import os

celery_app = Celery("commune-agent", broker=os.environ["REDIS_URL"])
client = CommuneClient(api_key=os.environ["COMMUNE_API_KEY"])

@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
)
def process_inbound_email(self, message_id, inbox_id, thread_id, sender_email, subject, body_text, metadata):
    # Prompt injection check (ADR-007)
    if metadata and metadata.get("prompt_injection_detected") is True:
        route_to_human_review(message_id=message_id, thread_id=thread_id)
        return

    # LLM call — no time constraint here
    reply_body = generate_reply(subject=subject, body=body_text)

    # Database write
    db.save_interaction(message_id=message_id, reply=reply_body)

    # Outbound send with idempotency key (ADR-006)
    client.messages.send(
        inbox_id=inbox_id,
        to=sender_email,
        subject=f"Re: {subject}",
        body=reply_body,
        thread_id=thread_id,
        idempotency_key=f"reply-{message_id}-{inbox_id}",
    )
```

**FastAPI + asyncio background tasks implementation (for simpler cases):**

```python
from fastapi import FastAPI, BackgroundTasks, Request
from commune import AsyncCommuneClient
import asyncio

app = FastAPI()
client = AsyncCommuneClient(api_key=os.environ["COMMUNE_API_KEY"])

async def process_email_background(payload):
    """Runs outside the request lifecycle — no 30s constraint."""
    message = payload.message
    if message.metadata and message.metadata.get("prompt_injection_detected"):
        await route_to_human_review(message)
        return

    reply = await generate_reply_async(message.body_text)
    await client.messages.send(
        inbox_id=message.inbox_id,
        to=message.from_email,
        subject=f"Re: {message.subject}",
        body=reply,
        thread_id=message.thread_id,
        idempotency_key=f"reply-{message.id}-{message.inbox_id}",
    )

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()

    try:
        payload = await client.webhooks.verify(raw_body, request.headers)
    except Exception:
        return {"error": "Invalid signature"}, 401

    # BackgroundTasks run after the response is sent
    background_tasks.add_task(process_email_background, payload)

    return {"status": "queued"}
```

Note: FastAPI `BackgroundTasks` run in the same process after response delivery but do not have dedicated worker processes. They are appropriate for agents with predictable, moderate processing times. For high-volume or long-running agents, use a dedicated worker process (Celery, RQ, or AWS SQS + Lambda) that can scale independently.

**Payload design: pass all needed context at enqueue time**

The background worker cannot make API calls back to Commune to retrieve the original message — the webhook payload is the authoritative source, and the handler will not re-execute. Every field the worker needs must be included in the enqueued payload:

- `message_id` (for idempotency key construction)
- `inbox_id` (for outbound send)
- `thread_id` (for thread continuity — ADR-001)
- `sender_email` and `subject` (for reply construction)
- `body_text` / `body_html` (the content to process)
- `metadata` (for injection detection — ADR-007)
- `extracted_data` (if using extraction schemas — ADR-003)

Do not store only the `message_id` and plan to fetch the full message in the worker — this creates an additional API call, adds latency, and introduces a failure mode if the message is not immediately available in the API.

---

## Alternatives Considered

**1. Synchronous processing with a fast LLM model constraint**

Use only fast models (GPT-4o-mini, Claude Haiku) with short max-token limits to keep response times under 10 seconds, staying within the 30-second window with margin.

This is a valid approach for narrow, specific agents and is appropriate for development and low-volume production. It fails under two conditions that are not within the operator's control:

- LLM API degradation: even "fast" models regularly exhibit P99 latencies above 30 seconds during OpenAI or Anthropic service degradation events. These events are unpublished and occur without warning
- Task complexity growth: agents often start simple and become more complex as requirements expand. A handler built for a fast, simple agent becomes a problem when tool use or multi-step reasoning is added

Synchronous processing is acceptable as a starting point for agents with consistently fast, bounded processing. It is not recommended as the long-term architecture for production agents.

**2. Streaming LLM responses as a latency mitigation**

Stream the LLM response to reduce time-to-first-token, then return 200 as soon as the stream starts. Problem: the 30-second window is measured from request start to response, not from stream start. More fundamentally, streaming reduces *perceived* latency for a human reader — it does not reduce the total time the handler is alive and occupying resources. The full processing (database write, outbound send) still happens after the stream completes.

Streaming is a useful technique for reducing latency in real-time user interfaces. It does not address the webhook timeout problem.

**3. Async handler (FastAPI + asyncio) as a replacement for background workers**

An `async def` FastAPI handler does not block threads while awaiting LLM responses — it releases the event loop to handle other requests. This improves concurrency significantly compared to a synchronous handler.

However, it does not eliminate the 30-second constraint: the handler is still alive and must return a response within 30 seconds regardless of whether it's blocking or not. For agents with multi-step LLM calls or orchestration that regularly exceeds 30 seconds, async does not solve the fundamental problem.

Async handlers are a valuable complementary approach for agents whose processing is bounded and fast. They are not a replacement for background workers when processing time is unbounded or exceeds the timeout threshold.

---

## Consequences

**Positive:**
- Webhook handlers consistently return 200 within milliseconds, regardless of LLM API latency or task complexity. Commune never retries due to handler timeout
- Background workers can be scaled independently of webhook handler capacity — add workers when processing throughput needs to increase, without changing the webhook tier
- Workers can be retried safely (with idempotency keys, per ADR-006) without triggering Commune redelivery or producing duplicate sends
- Webhook handler code is dramatically simpler: verify, enqueue, return. All complexity lives in the worker

**Negative:**
- Requires queue infrastructure in production. For Celery: Redis or RabbitMQ as broker, Celery worker processes, and a monitoring solution (Flower, or queue depth metrics). This infrastructure does not exist in most development environments, creating a gap between dev and prod that can hide queue-related bugs
- Processing is asynchronous — the agent's reply is delayed by queue depth and worker availability. If workers are saturated or the queue is backed up, reply latency grows. Operators must monitor queue depth and worker lag as primary health metrics
- Dead-letter queue management: failed background jobs that exhaust retries are lost unless routed to a dead-letter queue. Building dead-letter queue review and replay tooling is additional operational work
- The enqueued payload must be serializable (JSON or pickle). Attachments, binary content, and large email bodies may require streaming to object storage rather than inline enqueue. A message with a 10MB PDF attachment cannot be safely enqueued directly
- Error visibility: errors in background workers do not surface in the webhook response. Operators need centralized logging and alerting (Sentry, Datadog, CloudWatch) to observe worker failures — they will not see errors in webhook response logs

---

## Related

- **ADR-006** (idempotency keys): Background workers are only safe to retry because idempotency keys prevent duplicate email sends on retry. Without ADR-006, background worker retries are a duplicate-email risk equivalent to handler-level retries — the deduplication just happens deeper in the stack
- **ADR-001** (thread_id for conversation continuity): The `thread_id` from the inbound webhook payload must be included in the enqueued payload — the worker cannot retrieve it. If `thread_id` is not passed at enqueue time, the outbound reply will not be threaded, breaking conversation continuity
- **ADR-005** (sync vs. async client selection): The correct client type flows from the worker context. Celery tasks with the default prefork pool are synchronous processes — use `CommuneClient`. FastAPI `BackgroundTasks` run in an async context — use `AsyncCommuneClient`
