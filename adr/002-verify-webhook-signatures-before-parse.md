# ADR-002: Verify webhook signatures before parsing request body

**Status:** Accepted
**Date:** 2026-03-01
**Deciders:** Engineering team
**Technical area:** Webhook Security

## Context

Inbound email content is fully attacker-controlled. Anyone with an SMTP client can send an email to an agent inbox. Commune delivers inbound emails to a developer-configured HTTPS endpoint as JSON POST requests. This creates an attack surface: without signature verification, any HTTP client can POST to that endpoint with an arbitrary payload — effectively injecting messages into the agent as if they came from Commune.

The threat is concrete. An attacker who discovers a webhook URL (via DNS recon, log leaks, or public code repositories) can:
- Inject fabricated emails that trigger unintended agent behavior
- Replay legitimate captured webhook payloads to re-trigger actions
- Craft payloads that pass downstream validation but contain injected LLM instructions (prompt injection)

Commune signs each webhook delivery using HMAC-SHA256. The signature covers the raw request body concatenated with a Unix millisecond timestamp: `HMAC(secret, "{timestamp}.{raw_body}")`. This signature is sent as the `X-Commune-Signature` header in the format `v1={hex_digest}`, and the timestamp is sent separately as `X-Commune-Timestamp`.

There is a critical ordering constraint. The HMAC is computed over the **raw request bytes** — the exact byte sequence as transmitted by Commune. If the application parses the JSON body first (e.g., `data = request.json()`) and then re-serializes it (e.g., `json.dumps(data).encode()`), the resulting bytes will typically differ from the original. JSON serializers differ in key ordering behavior, whitespace insertion, and Unicode normalization. The consequence of parsing before verifying is either:
- Valid Commune webhooks that fail verification (false rejections), or
- Subtly crafted payloads where an attacker re-orders JSON keys to produce a different semantic meaning while preserving a captured HMAC (if re-serialization is used as the verification input)

An additional complication arises from HTTP framework design. Flask's `request.json` property reads from the request body stream. Once read, the stream position advances and the raw bytes are no longer accessible via `request.get_data()`. The raw bytes must be captured **before** any framework convenience method that reads the body.

Framework-specific patterns:
- Flask: `raw = request.get_data()` (must precede `request.json`)
- FastAPI: `raw = await request.body()`
- Django: `raw = request.body`

## Decision

`verify_signature()` must be called with raw request bytes before any JSON parsing. The function signature enforces this intent: it accepts `payload: bytes | str` (the raw body), not a parsed dict. The timestamp header must be passed to enable replay attack protection within the 5-minute tolerance window. Webhook handlers that fail verification return HTTP 401 immediately without inspecting or acting on the payload.

```python
from commune import CommuneClient
client = CommuneClient(api_key="...")

# Flask example — raw bytes FIRST
@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body  = request.get_data()          # capture before request.json
    signature = request.headers.get("X-Commune-Signature", "")
    timestamp = request.headers.get("X-Commune-Timestamp", "")

    if not client.verify_signature(
        payload=raw_body,
        signature=signature,
        secret=WEBHOOK_SECRET,
        timestamp=timestamp,
    ):
        return "", 401                       # reject without parsing payload

    data = json.loads(raw_body)             # safe to parse now
    # ... handle event
```

## Consequences

### Positive
- Webhooks are unforgeable: only Commune, possessing the shared secret, can generate valid signatures.
- Replay attacks are prevented: the 5-minute timestamp window (`tolerance_seconds=300`) blocks retransmission of captured valid payloads.
- Prompt injection via crafted webhook POST requests is blocked at the transport layer before payload content reaches agent logic (see ADR-007).
- The verification check is stateless and adds sub-millisecond latency — no external calls required.

### Negative
- **Boilerplate on every handler**: raw bytes must be explicitly captured before any framework body-parsing call. Developers unfamiliar with stream consumption semantics will get this wrong, producing mysterious "valid webhook fails verification" bugs.
- **Framework-specific patterns**: the correct raw-bytes capture call differs across Flask, FastAPI, Django, aiohttp, and others. There is no single line of code that works everywhere. Documentation must cover each major framework explicitly.
- **Silent failure is the default**: a webhook handler that returns 401 produces no visible output in the agent. Unless the developer explicitly logs failed verifications or sets up alerting, attack attempts are invisible in production. This makes it hard to distinguish "no attacks" from "attacks happening but unmonitored."
- **Secret rotation is operationally non-trivial**: rotating the webhook secret requires coordinating a new secret in Commune with a deployment of updated application configuration. During the window between secret update and deployment, verification will fail for all valid webhooks.

### Neutral
- The 5-minute replay window (`tolerance_seconds=300`) is configurable. Systems with clock skew or high-latency webhook delivery may need to increase this, which widens the replay attack window.

## Alternatives Considered

### Option A: Verify after JSON parsing (re-serialize to bytes)
Parse the JSON body first, then re-serialize to bytes for HMAC verification: `json.dumps(parsed).encode()`.

**Rejected because:** JSON serialization is not byte-for-byte deterministic across implementations, Python versions, or key ordering configurations. `json.dumps` in Python 3.7+ preserves insertion order, but insertion order from a parsed JSON string depends on the parser. Unicode normalization (e.g., `\u0041` vs `A`) can also differ. Even within a single Python version, this approach is fragile and creates a false sense of security: the HMAC is being verified against re-serialized bytes, not the original transmitted bytes.

### Option B: IP allowlisting (only accept POST requests from Commune's IP range)
Maintain a list of Commune's outbound IP addresses and reject requests from other origins at the network or application layer.

**Rejected because:** Commune's outbound IP addresses are not guaranteed to be static — they rotate with infrastructure changes, CDN edges, and failover events. Allowlist maintenance is an ongoing operational burden. More critically, IP allowlisting does not prevent SSRF-based attacks where an internal service is tricked into making requests to the webhook endpoint. HMAC verification is cryptographically sound regardless of network topology.

### Option C: Mutual TLS (mTLS)
Require Commune to present a client certificate when making webhook deliveries. Verify the certificate at the application or load balancer layer.

**Rejected because:** mTLS requires certificate infrastructure on both sides — Commune must manage a CA and issue certificates, and developers must configure certificate verification in their applications or infrastructure. This is a significantly higher operational burden than a shared HMAC secret for an equivalent security outcome. HMAC-SHA256 is well within the required security margin for this threat model.

## Related Decisions

- [ADR-007: Prompt injection defense](007-prompt-injection-defense.md) — signature verification is a prerequisite; prompt injection defense applies to content that has already passed verification.
- [ADR-004: One inbox per agent identity](004-one-inbox-per-agent-identity.md) — each inbox has its own webhook secret; applications with multiple inboxes must route to the correct secret before verification.

## Notes

The `v1=` prefix in the `X-Commune-Signature` header is a version prefix, following the pattern established by Stripe's webhook signatures. Future signature algorithm changes (e.g., if HMAC-SHA256 is deprecated) can introduce a `v2=` variant without breaking existing handlers that check for `v1=`.

The `verify_signature()` function raises no exceptions on invalid input — it returns `False`. Callers should not rely on exceptions for control flow.
