# ADR-001: Use thread_id for all reply flows

**Status:** Accepted
**Date:** 2026-03-01
**Deciders:** Engineering team
**Technical area:** SDK / Agent Architecture

## Context

Agents need to conduct multi-turn conversations over email: they receive an inbound message, reply, the human replies back, and the agent replies again. This pattern is fundamental to agentic email workflows — support triage, scheduling, hiring, research outreach.

Email threading is governed by RFC 5322. Clients like Gmail, Outlook, and Apple Mail use the `In-Reply-To` and `References` headers to group messages into a visible conversation thread. When these headers are absent or incorrect, each reply appears as a new email, stripping the human of context and making the conversation unmanageable in their inbox. For automated agents sending dozens of replies, this is not a minor UX issue — it renders the agent non-functional in practice.

There are three ways to maintain threading:

1. **Manual header setting** — the application developer explicitly passes `In-Reply-To` and `References` values on each send. This is technically correct but requires RFC 5322 knowledge from application developers, is error-prone (the References chain must be accumulated correctly), and puts the burden of header management on every caller.
2. **Subject-based matching** — the server groups messages by subject line (the "Re: " prefix convention). This is what many consumer email apps do implicitly, but it breaks when subjects are edited, when the human starts a new email about the same topic, and when cross-thread collisions occur (two people named Alice both emailing about "Scheduling").
3. **thread_id parameter** — the SDK surfaces a stable `thread_id` that the server uses to look up and apply the correct `In-Reply-To` and `References` headers automatically. The agent stores `thread_id`; the server owns header correctness.

The `thread_id` value is available to callers in two places: in the `SendMessageResult` returned by `messages.send()` after the first send, and in the webhook payload for every inbound email. This means agents always have access to it at the point where they need to reply — no additional API calls required.

The SDK must make correct threading the default path, not an opt-in. A developer who forgets to think about threading should still produce correct behavior if they pass `thread_id` through.

## Decision

`thread_id` is a first-class parameter on `messages.send()`. For reply flows, it is the primary and recommended mechanism for maintaining RFC 5322 threading. When `thread_id` is present, the Commune server sets `In-Reply-To` and `References` headers automatically and correctly. Agents are responsible for persisting `thread_id` between the inbound webhook and the subsequent send.

```python
# Inbound webhook handler
thread_id = payload["thread_id"]       # always present
inbox_id  = payload["inbox_id"]        # identifies which agent should handle this

# Reply
result = client.messages.send(
    inbox_id=inbox_id,
    to=payload["from"],
    subject=payload["subject"],
    text="...",
    thread_id=thread_id,               # maintains RFC 5322 thread
)
```

## Consequences

### Positive
- RFC 5322 threading works correctly in all major email clients (Gmail, Outlook, Apple Mail) without application developers learning header semantics.
- Agents have no dependency on email header knowledge — `thread_id` is an opaque string.
- `thread_id` is present on `SendMessageResult` after the first send, so callers always have it for subsequent replies without additional API calls.
- Server-side header management means correctness improvements (e.g., accumulating the References chain) are transparent to SDK callers.

### Negative
- **Stateful requirement**: agents must store `thread_id` somewhere — Redis, a database, or in-memory — between the inbound webhook and the outbound send. For stateless serverless functions, this requires an external store, adding infrastructure complexity.
- **No recovery on loss**: if `thread_id` is lost (process restart, cache eviction, database failure), subsequent sends start new threads. There is no mechanism to re-attach a send to an existing thread after the fact.
- **Lifetime coupling**: `thread_id` is scoped to an inbox. If an inbox is deleted and recreated, thread continuity is broken even if the thread_id value is stored correctly.

### Neutral
- Agents that start a new conversation (no inbound message first) naturally have no `thread_id` initially. The first `messages.send()` returns a `thread_id` that the agent should store for future follow-ups.

## Alternatives Considered

### Option A: Manual In-Reply-To / References header setting
Expose raw header fields as parameters on `messages.send()`. Application developers construct the correct References chain themselves.

**Rejected because:** RFC 5322 References header accumulation is subtle (the chain must include all prior Message-IDs in the thread, not just the immediate parent). Requiring this from application developers guarantees correctness bugs in practice. It also creates an API surface that must track email standards changes.

### Option B: Subject-based threading
The server implicitly groups messages by subject line (stripping "Re: " prefix). No thread_id required from callers.

**Rejected because:** Brittle across real-world email behavior. Humans frequently change subjects mid-thread, start new emails about the same topic, or use identical subjects for unrelated conversations. Cross-thread collisions are undetectable by the server. Also fails entirely for non-reply webhook events (delivery status, bounces) where threading context is irrelevant.

### Option C: Auto-detect threading via In-Reply-To header on inbound messages
On inbound email receipt, extract the `In-Reply-To` header and automatically associate the reply. Surface this association transparently so agents don't need to manage thread_id.

**Rejected because:** Only works for direct reply chains. Fails when humans start new emails about the same topic (no In-Reply-To set). Also doesn't solve the outbound problem — the agent still needs to know which thread to attach its reply to, which requires the same stateful persistence.

## Related Decisions

- [ADR-006: Idempotency keys for send deduplication](006-idempotency-keys.md) — thread_id persistence and idempotency_key generation face the same statefulness requirement; both should use the same backing store.
- [ADR-004: One inbox per agent identity](004-one-inbox-per-agent-identity.md) — thread_id is scoped to an inbox; multi-inbox architectures must track which inbox a thread_id belongs to.

## Notes

The `thread_id` field in inbound webhook payloads refers to the Commune thread, not an email Message-ID. Agents should not attempt to parse or construct `thread_id` values — treat as an opaque string.

Open question: should a future SDK version surface a `conversation` abstraction that encapsulates both `thread_id` persistence and the send/receive loop? This would eliminate the stateful burden from application code at the cost of reduced flexibility.
