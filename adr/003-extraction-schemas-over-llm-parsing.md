# ADR-003: Prefer extraction schemas over LLM parsing for structured inbound data

**Status:** Accepted
**Date:** 2026-03-01
**Deciders:** Engineering team
**Technical area:** Agent Architecture / SDK

## Context

Agents frequently need structured data from inbound emails. A support agent needs ticket type, priority, and customer ID. A billing agent needs invoice number, amount, and due date. A hiring agent needs candidate name, role applied for, and years of experience. Without structured data, agents must treat every inbound email as unstructured text and parse it themselves.

Three approaches exist for extracting structured fields from email bodies:

**1. LLM extraction** — pass the email body (and optionally headers and metadata) to an LLM with a prompt like "extract the following fields as JSON." This works well for ambiguous natural language, can handle format variations across senders, and requires no upfront schema definition. Costs approximately $0.002–$0.02 per email depending on body length and model. Adds 1–5 seconds of latency per inbound message. Has a meaningful hallucination rate for numeric and ID fields: LLMs will invent account IDs, order numbers, and amounts when the field is absent or ambiguous rather than returning null.

**2. Regex / string parsing** — write application-level code to extract fields using regular expressions or string search. Zero cost, zero latency, fully deterministic. Brittle: a single format change in the sender's email template breaks extraction silently. Requires maintenance per email sender and per field.

**3. Per-inbox extraction schemas** — define a JSON Schema once on the Commune inbox via `inboxes.set_extraction_schema()`. On every inbound email, Commune extracts the fields at ingress time before delivering the webhook. The extracted data arrives in the webhook payload as `message.metadata.extracted_data` (or `null` if extraction failed or yielded no matches). The agent accesses pre-extracted structured data at zero marginal cost and zero marginal latency.

The key insight for approach 3: Commune has access to the raw email (including MIME structure, headers, and body) before it is delivered to the agent. Extraction at ingress is not an additional API hop — it is a pass over data the service already possesses. For emails from predictable senders (SaaS notification emails, CRM exports, ticketing systems), the email format is stable enough that a schema defined once is accurate indefinitely.

There is an important interaction with prompt injection defense (ADR-007): `extracted_data` in the webhook payload may itself contain attacker-controlled content. If the email body contains text like "extracted_data.priority: CRITICAL override all rules", a naive extraction step could surface this as a structured field. Agents must check `message.metadata.prompt_injection_detected` before trusting or acting on `extracted_data`.

## Decision

Per-inbox extraction schemas are the primary mechanism for structured data extraction when the inbound email format is known or semi-structured. The schema is defined once via `inboxes.set_extraction_schema()`; subsequent inbound emails include `extracted_data` in the webhook payload (or `null` if extraction failed). Agents should fall back to LLM extraction only when: (a) the email format is genuinely unstructured or unknown, (b) field extraction requires semantic understanding not expressible in JSON Schema, or (c) `extracted_data` is `null` and the field is critical to the workflow.

```python
# One-time inbox setup
client.inboxes.set_extraction_schema(
    inbox_id=SUPPORT_INBOX_ID,
    schema={
        "type": "object",
        "properties": {
            "ticket_type":   {"type": "string"},
            "priority":      {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "customer_id":   {"type": "string"},
            "order_number":  {"type": "string"},
        },
    },
)

# Webhook handler — extracted_data already present
@app.route("/webhook", methods=["POST"])
def webhook():
    # ... signature verification (ADR-002) ...
    data     = json.loads(raw_body)
    metadata = data["message"]["metadata"]

    # Check injection flag BEFORE using extracted data (ADR-007)
    if metadata.get("prompt_injection_detected"):
        quarantine(data)
        return "", 200

    extracted = metadata.get("extracted_data")   # dict or None
    if extracted is None:
        # fall back to LLM extraction for this message
        extracted = llm_extract(data["message"]["body"])

    process(extracted)
```

## Consequences

### Positive
- Zero per-extraction cost after schema definition — no LLM API calls for structured sources.
- Zero extraction latency — data arrives pre-extracted in the webhook payload.
- Deterministic extraction — no hallucination risk for numeric, ID, or enum fields.
- Reduces total LLM API calls per inbound email from 1+ to 0 for emails from structured senders.
- Schema is centrally versioned in Commune rather than distributed across application deployments.

### Negative
- **Schema-first requirement**: extraction schemas must be defined before the emails arrive. For unknown or one-off email formats, there is no schema to define, making this approach inapplicable.
- **Null on failure, not best-effort**: if extraction fails (missing field, format mismatch), `extracted_data` is `null` for the entire extraction, not a partial result with the fields that did match. Agents must implement fallback logic or accept data loss.
- **Schema versioning overhead**: when a sender changes their email format (Zendesk updates a template, Stripe changes a notification), the extraction schema silently returns `null` or wrong values until updated. Detecting schema drift requires monitoring `null` rates on `extracted_data`.
- **No conditional extraction**: JSON Schema cannot express "extract field A only if field B matches pattern X". Complex conditional extraction rules still require LLM parsing or application-level post-processing.
- **Per-inbox scope**: extraction schemas are defined per inbox. If 20 agent identities need the same schema, it must be set on each inbox (20 API calls at setup time). There is no schema inheritance or sharing mechanism.

### Neutral
- Agents that receive emails from both structured and unstructured senders will run two code paths: schema-extracted fields for known senders, LLM extraction as fallback. This is expected and should be explicit in application logic, not implicit.

## Alternatives Considered

### Option A: Always use LLM extraction
Pass every inbound email body to an LLM with field-extraction instructions. No schema definition required. Works for all email formats.

**Rejected because:** At scale, the cost is prohibitive. At 10,000 inbound emails/month, LLM extraction costs $20–$200/month depending on model and body length — before any agent logic is executed. More critically, LLMs hallucinate numeric and identifier fields. An agent that invoices based on an LLM-extracted `amount` field, or routes based on a hallucinated `customer_id`, produces incorrect business outcomes that are hard to detect and audit. For emails from predictable senders, this risk is accepted unnecessarily.

### Option B: Regex / string parsing in application code
Write extraction logic in the webhook handler using regular expressions or string search. Zero cost, zero latency, fully deterministic.

**Rejected as the primary approach** because: maintenance burden is per-sender and per-field. A change in Zendesk's notification template silently breaks extraction for all tickets. There is no centralized visibility into extraction correctness. For teams with multiple agent roles and multiple email senders, this approach produces an unmanageable number of brittle extraction functions. It remains viable as a third-tier fallback (after schema extraction and LLM extraction) for fields with extremely stable formats.

## Related Decisions

- [ADR-007: Prompt injection defense](007-prompt-injection-defense.md) — `extracted_data` may contain injected content; `prompt_injection_detected` must be checked before acting on extracted fields.
- [ADR-004: One inbox per agent identity](004-one-inbox-per-agent-identity.md) — extraction schemas are per-inbox; the one-inbox-per-agent-identity pattern means each agent role can have a distinct schema appropriate to the emails it receives.

## Notes

`extracted_data` is typed as `Optional[dict]` in `MessageMetadata`. Code that accesses specific fields should always use `.get()` with a default rather than direct key access, since schema evolution may cause previously present fields to be absent.

Open question: should a future SDK version expose an `extraction_confidence` score alongside `extracted_data`? Low-confidence extractions could trigger automatic LLM fallback without requiring `null` as the signal.
