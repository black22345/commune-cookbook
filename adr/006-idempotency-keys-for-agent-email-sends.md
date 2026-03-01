# ADR-006: Idempotency Keys for Agent Email Sends

**Status:** Accepted
**Date:** 2026-03-01
**Technical Area:** Reliability / Distributed Systems

---

## Context

AI agents operate in delivery systems that guarantee at-least-once execution, not exactly-once. This is a fundamental property of every reliable queue and webhook delivery mechanism:

- Commune delivers webhooks with at-least-once semantics. If a handler returns a non-2xx response — or times out — Commune redelivers the webhook, typically with exponential backoff (5s, 30s, 5m, 30m, 2h)
- Celery and RQ retry tasks on exception. A task that sends an email, then raises an uncaught exception during the database write that follows, will be retried. The email has already been sent; on retry, it will be sent again
- AWS Lambda has at-least-once invocation semantics for event source mappings (SQS, SNS, Kinesis). A Lambda handler that crashes partway through may be reinvoked with the same event
- HTTP clients with retry logic (including `httpx` with transport retries configured) may replay a request if the network layer drops the response after the server processed it

The compound effect: a single logical "agent sends a reply email" operation may be attempted 2–5 times in a degraded-but-operational system. Without deduplication, each attempt produces a separate outbound email — the recipient receives 2–5 identical messages.

This is not a hypothetical concern. Email duplication is among the highest-severity agent failures in production because it is immediately visible to end users, erodes trust, and cannot be recalled. A duplicated support acknowledgement is an annoyance; a duplicated "your order has shipped" email causes customer service load; a duplicated password reset email creates confusion and potential security questions.

The root cause is a classic distributed systems problem: the side effect (email send) is not atomic with the acknowledgement (HTTP 200, task success signal). Any failure between the side effect and the acknowledgement causes the operation to be retried from before the side effect.

**The standard solution is idempotency keys.** An idempotency key is a caller-supplied token that the server uses to deduplicate requests: if two requests arrive with the same key, the server processes the first and returns the cached result for the second without repeating the side effect.

Commune's `messages.send()` accepts an `idempotency_key: str` parameter. Within a 24-hour window, a duplicate send with the same key returns the original `SendMessageResult` (including the original `message_id`) without delivering a second email.

The key design constraint is that idempotency keys must be **deterministic from the triggering event**: derivable from data that survives across retries without requiring shared mutable state. A key like `str(uuid.uuid4())` is useless — it generates a different key on every invocation. A key derived from the inbound message ID and the agent's identifier is stable: the same inputs produce the same key on every retry.

---

## Decision

All agent-initiated email sends include an `idempotency_key` derived deterministically from the triggering event context.

**Standard patterns by send type:**

**1. Reply to an inbound message (most common):**

```python
result = client.messages.send(
    inbox_id=inbox_id,
    to=reply_to_address,
    subject=reply_subject,
    body=reply_body,
    thread_id=thread_id,                          # ADR-001
    idempotency_key=f"reply-{source_message_id}-{agent_id}",
)
```

`source_message_id` is `payload.message.id` from the webhook — stable across redeliveries of the same webhook. `agent_id` is the agent's identifier (inbox ID, agent name, or a configured constant).

**2. Outbound campaign or notification (not triggered by inbound):**

```python
result = client.messages.send(
    inbox_id=inbox_id,
    to=recipient_email,
    subject=subject,
    body=body,
    idempotency_key=f"campaign-{campaign_id}-{recipient_id}-{send_date}",
)
```

`send_date` (ISO date string, e.g., `"2026-03-01"`) prevents deduplication from suppressing legitimate daily sends to the same recipient.

**3. Multi-step agent sends (agent sends multiple emails per trigger):**

```python
# Agent sends acknowledgment AND a detailed follow-up for the same inbound message
ack_result = client.messages.send(
    ...,
    idempotency_key=f"ack-{source_message_id}-{agent_id}",
)
followup_result = client.messages.send(
    ...,
    idempotency_key=f"followup-{source_message_id}-{agent_id}",
)
```

Each send within a single trigger gets a distinct key prefix. Using the same key for both would deduplicate the second send incorrectly.

**Key construction rules:**
1. Keys must be stable across retries — derived only from event data, not time, random values, or local state
2. Keys must be unique per intended email — include enough specificity to distinguish intentional re-sends
3. Keys must be scoped to the triggering event — include the source message ID or equivalent stable event identifier
4. Maximum key length is 255 characters; use hashing if constructing from long fields:

```python
import hashlib

def make_idempotency_key(*parts: str) -> str:
    raw = "-".join(parts)
    if len(raw) <= 255:
        return raw
    # Hash to 64-char hex when raw key would exceed limit
    return hashlib.sha256(raw.encode()).hexdigest()
```

---

## Alternatives Considered

**1. Application-level deduplication via a shared store (Redis, DynamoDB)**

Track sent `message_id` values in a distributed cache with TTL. Before sending, check if `(source_message_id, agent_id)` is in the cache; if yes, skip the send.

This approach works but has three structural problems:

- **Race condition**: the check-then-send sequence is not atomic. Two concurrent retries can both pass the check before either records the send. The race window is small (tens of milliseconds) but real in high-throughput systems
- **Operational dependency**: requires a running Redis/DynamoDB instance, key expiration policy, and handling of cache unavailability (fail open? fail closed?)
- **Divergence risk**: the cache record is a proxy for "email was sent" — it can be wrong. If the cache is flushed or expires, the deduplication constraint is lost for in-flight operations

Server-side idempotency keys avoid all three: the deduplication state lives with the authoritative system (Commune's backend), is atomic with the send operation, and doesn't require any local infrastructure.

**2. Rely on Commune's webhook deduplication only**

Commune deduplicates webhook delivery: if a webhook was successfully delivered (handler returned 200), it will not be redelivered for the same event. This is a related but distinct guarantee.

The gap: a handler that sends an email at line 10 and crashes at line 20 (before returning 200) has already sent the email but not acknowledged receipt. Commune will redeliver the webhook. The handler will send the email again. Webhook deduplication does not protect against this — it only prevents redelivery of already-acknowledged events.

**3. Transactional outbox pattern**

Write a "pending send" record to the database in the same transaction as other state changes, then have a separate process poll for pending sends and execute them. Classic distributed systems pattern for at-most-once side effects.

More reliable than idempotency keys for complex multi-step operations, but significantly more infrastructure to operate. Appropriate for critical financial or compliance workflows; disproportionate for typical agent email sends. Not mutually exclusive — idempotency keys remain useful even in an outbox architecture.

---

## Consequences

**Positive:**
- At-most-once email delivery guaranteed within the 24-hour deduplication window, regardless of retry behavior in the surrounding infrastructure
- Webhook handlers and background workers are safe to retry without user-visible side effects — the idempotency guarantee absorbs the retry
- No shared distributed state required between retries — keys are derived from event data, not stored anywhere
- The original `SendMessageResult` (including `message_id`) is returned on duplicate calls, so downstream code that stores the `message_id` behaves correctly on retry

**Negative:**
- The 24-hour deduplication window means that two distinct, intentional sends with the same key within one day will result in only one email delivered. Key construction must include enough specificity to distinguish intentional re-sends. A support agent that legitimately needs to resend a reply to the same inbound message must vary the key (e.g., append a resend counter)
- Key uniqueness is the caller's responsibility. The SDK accepts any string as `idempotency_key` — it performs no validation that the key is well-formed or sufficiently specific. A poorly chosen key (e.g., `f"send-{agent_id}"` without message scoping) silently drops all sends after the first within the deduplication window
- The parameter is optional in the SDK's type signature, making it easy to omit. The only enforcement mechanism is code review and linting conventions, not the type system. A `CommuneClient` configured with `require_idempotency_keys=True` (hypothetical) could enforce this at the SDK level, but no such option currently exists

---

## Related

- **ADR-001** (thread_id for conversation continuity): Idempotency keys must be scoped per-message, not per-thread. Two different inbound messages in the same thread that each trigger a reply need distinct idempotency keys. If you use `thread_id` as the only scope in the key, the second reply will be silently deduplicated
- **ADR-008** (background processing): Background workers can safely retry failed operations because idempotency keys absorb the duplicate send. Without idempotency keys, background task retries are unsafe — they must be configured to never retry, losing the reliability benefit of background processing entirely
