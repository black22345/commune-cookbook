# ADR-004: One inbox per agent identity

**Status:** Accepted
**Date:** 2026-03-01
**Deciders:** Engineering team
**Technical area:** Agent Architecture

## Context

Multi-agent systems tend toward shared infrastructure to reduce complexity: one database, one queue, one email inbox with centralized dispatch. This pattern works well for human-operated systems where an operator reads all incoming messages and manually routes them. It breaks down for autonomous agents.

With a shared inbox, every inbound email fires a single webhook endpoint that must route to the correct agent. Routing logic must inspect the payload and decide which agent handles it. The practical routing signals available are:

- **Subject line**: `"Re: Partnership inquiry"` → PartnershipAgent. Fails when a human changes the subject mid-thread ("Following up on our meeting"), when two conversations share a subject, or for non-email webhook events (delivery confirmations, bounce notifications) that have no subject.
- **Sender address**: `zendesk@company.com` → SupportAgent. Fails when a single sender communicates with multiple agents about different topics, or when the relevant context is in the body rather than the sender.
- **Body content matching**: regex or LLM classification of email body. Adds latency (LLM) or brittleness (regex) to every inbound message. Classification errors — inherent in probabilistic approaches — cause messages to be routed to the wrong agent, silently.
- **In-Reply-To header**: can reconstruct thread ownership from a prior outbound message. Requires the application to maintain a mapping from Message-IDs to agent identities — a stateful index with its own failure modes.

Per-inbox isolation eliminates routing as a problem category. Each logical agent identity — SupportAgent, ResearchAgent, HiringAgent, PartnershipAgent — has its own inbox with its own dedicated webhook endpoint. The `inbox_id` in every webhook payload (including delivery status events and bounces, not just inbound messages) deterministically identifies which agent receives the event. Routing is O(1) and has no failure modes that require application-level handling.

For multi-tenant SaaS products, the natural boundary is per tenant. A tenant's emails should not be visible to other tenants' agents. This also means per-tenant extraction schemas, per-tenant webhook secrets, and per-tenant audit trails — all of which are properties of the inbox, not the agent role.

The cost of this approach scales with the number of agent identities and tenants. A system with 5 agent roles and 1,000 tenants requires 5,000 inboxes. Inbox creation must be programmatic (part of tenant onboarding automation), not manual. This is an operational consideration that must be accounted for in the system design upfront.

## Decision

Each logical agent identity gets its own inbox. In multi-tenant applications, each tenant gets one inbox per agent role they use. Routing at the webhook boundary is done exclusively by `inbox_id` — never by subject, sender, or body content. Inbox creation is programmatic; applications must create inboxes as part of agent initialization or tenant onboarding flows.

```python
# Tenant onboarding — create one inbox per agent role
def onboard_tenant(tenant_id: str) -> dict:
    support_inbox = client.inboxes.create(
        name=f"support-{tenant_id}",
        webhook_url=f"https://api.example.com/webhooks/support/{tenant_id}",
    )
    research_inbox = client.inboxes.create(
        name=f"research-{tenant_id}",
        webhook_url=f"https://api.example.com/webhooks/research/{tenant_id}",
    )
    # Store inbox IDs associated with tenant
    return {
        "tenant_id":       tenant_id,
        "support_inbox":   support_inbox.id,
        "research_inbox":  research_inbox.id,
    }

# Webhook handler — routing is done by URL path, not payload inspection
@app.route("/webhooks/support/<tenant_id>", methods=["POST"])
def support_webhook(tenant_id: str):
    # inbox_id in payload confirms identity — no routing logic needed
    support_agent.handle(tenant_id, request)

@app.route("/webhooks/research/<tenant_id>", methods=["POST"])
def research_webhook(tenant_id: str):
    research_agent.handle(tenant_id, request)
```

## Consequences

### Positive
- Routing is O(1) and deterministic — `inbox_id` is always present in every webhook event type, including delivery status events and bounces that carry no email content.
- Independent failure isolation — a bug in SupportAgent's webhook handler does not affect ResearchAgent's inbox queue or processing.
- Per-agent audit trail — `threads.list(inbox_id=support_inbox_id)` returns exactly what the support agent has handled, with no filtering required.
- Per-agent extraction schemas — SupportAgent can extract `ticket_type` and `priority`, HiringAgent can extract `candidate_name` and `role`, without schema conflicts (see ADR-003).
- Per-inbox webhook secrets — an exposed secret for one agent role requires rotating only that inbox's secret, not all inboxes.

### Negative
- **Inbox count scales with agents × tenants**: 5 roles × 10,000 tenants = 50,000 inboxes. At this scale, inbox management (creation, deletion at churn, secret rotation, monitoring) must be fully automated. Manual management is not viable.
- **Onboarding step required**: inbox creation must succeed before the agent can receive email. This adds a synchronous external API call to tenant onboarding flows, a new failure mode (Commune API unavailable during onboarding) that must be handled with retries.
- **Thread continuity breaks on inbox deletion**: `thread_id` is scoped to the inbox in which it was created. If a tenant's inbox is deleted (e.g., on plan downgrade) and recreated (on re-activation), stored `thread_id` values from before deletion are invalid. In-flight conversations lose continuity with no recovery path.
- **No cross-agent thread continuity**: if a conversation starts in SupportAgent and needs to be handed off to an EscalationAgent, the thread_id cannot be directly transferred between inboxes. Handoffs require starting a new thread in the destination inbox.

### Neutral
- Webhook URL design is a product decision: URLs can encode tenant_id in the path (as shown above) or rely solely on the `inbox_id` in the payload for identification. Path-encoded tenant_id is more debuggable; payload-only identification requires fewer URL patterns.

## Alternatives Considered

### Option A: One inbox per application, route by subject or sender
Maintain a single inbox and implement routing logic in the webhook handler based on subject line, sender address, or body content.

**Rejected because:** All three routing signals are unreliable (see Context). Subject changes break thread ownership. Sender-based routing fails for senders that communicate with multiple agent roles. Body classification adds latency and introduces routing errors. For a system that must handle production traffic reliably, probabilistic routing is not acceptable when deterministic routing (by inbox_id) is available.

### Option B: One inbox per tenant, route by body content to agent roles
Each tenant has one inbox; a classifier in the webhook handler dispatches to the correct agent role.

**Rejected because:** Classification accuracy is bounded below 100%, and misrouted messages produce silent incorrect behavior. It also prevents per-agent extraction schemas — a single inbox must use a single schema, which must be a union of all agent roles' fields. Schema conflicts (e.g., SupportAgent and HiringAgent both wanting a field named `priority` with different enum values) have no clean resolution. Cross-tenant isolation is preserved, but cross-role isolation is not.

### Option C: One inbox per tenant, route by message tagging / labels
Callers tag outbound messages with metadata that is echoed in reply webhooks, enabling routing by tag rather than by inbox.

**Rejected because:** This only works for replies to outbound messages (where the agent set the tag). Unsolicited inbound emails (a human emailing the agent directly) arrive with no tag. Also requires the tag to survive email transit, which is not guaranteed — many email clients and servers strip or modify custom headers.

## Related Decisions

- [ADR-001: Use thread_id for all reply flows](001-use-thread-id-for-all-replies.md) — thread_id is scoped to an inbox; the one-inbox-per-identity pattern ensures thread_id uniqueness is meaningful.
- [ADR-003: Prefer extraction schemas over LLM parsing](003-extraction-schemas-over-llm-parsing.md) — extraction schemas are per-inbox; this pattern enables each agent role to have a distinct, appropriate schema.

## Notes

Inbox deletion behavior on `threads.list()` and `messages.send()` with a deleted inbox_id should return a well-typed error (e.g., `InboxNotFoundError`) rather than a silent 404. Applications should surface this as a recoverable error that triggers inbox recreation via the onboarding flow.

Future consideration: an inbox "suspension" state (inbox exists, webhook delivery paused) would allow plan downgrades without losing thread continuity. This would preserve `thread_id` validity while preventing new webhook deliveries.
