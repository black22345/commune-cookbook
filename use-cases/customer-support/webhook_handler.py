"""
Webhook handler for customer support email agent — powered by Commune + OpenAI

Receives inbound email events from Commune, verifies the signature,
generates an AI reply, and sends it back to the customer.

Install:
    pip install flask openai commune-mail

Usage:
    export COMMUNE_API_KEY=comm_...
    export OPENAI_API_KEY=sk-...
    export COMMUNE_WEBHOOK_SECRET=whsec_...
    python webhook_handler.py
"""

import json
import logging
import os
import threading

from flask import Flask, request, jsonify
from openai import OpenAI
from commune import CommuneClient
from commune.webhooks import verify_signature, WebhookVerificationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

COMMUNE_API_KEY = os.environ["COMMUNE_API_KEY"]
OPENAI_API_KEY  = os.environ["OPENAI_API_KEY"]
WEBHOOK_SECRET  = os.environ["COMMUNE_WEBHOOK_SECRET"]

commune = CommuneClient(api_key=COMMUNE_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """You are a helpful customer support agent for Acme SaaS.
Reply professionally and concisely. Always sign off as '— Acme Support'.
Do not mention you are an AI unless directly asked."""


def generate_and_send_reply(
    sender: str,
    subject: str,
    body: str,
    thread_id: str,
    inbox_id: str,
) -> None:
    """
    Generate an AI reply and send it. Runs in a background thread so the
    webhook handler can return HTTP 200 immediately without blocking on
    the LLM call (which typically takes 3-8 s).
    """
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Subject: {subject}\n\n"
                        f"Customer message:\n{body}"
                    ),
                },
            ],
        )
        reply_text = completion.choices[0].message.content.strip()
        logger.info(f"Generated reply ({len(reply_text)} chars) for thread {thread_id}")

        commune.messages.send(
            to=sender,
            subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
            text=reply_text,
            inbox_id=inbox_id,
            thread_id=thread_id,   # maintains conversation thread
        )
        logger.info(f"Reply sent to {sender} on thread {thread_id}")

    except Exception:
        logger.exception(f"Failed to generate/send reply for thread {thread_id}")


@app.route("/webhook/commune", methods=["POST"])
def handle_webhook():
    """
    Main webhook endpoint.

    Reads raw bytes BEFORE any JSON parsing — HMAC is computed over the
    exact bytes Commune sent. Re-serializing a parsed dict changes whitespace
    and key order, breaking signature verification.

    Returns HTTP 200 in < 50 ms regardless of payload size; all LLM work
    happens in a background thread.
    """
    # Read raw bytes first — required for correct HMAC verification
    raw_body: bytes = request.get_data()
    signature: str = request.headers.get("X-Commune-Signature", "")
    timestamp: str = request.headers.get("X-Commune-Timestamp", "")

    if not signature:
        logger.warning("Webhook received without signature — rejecting")
        return jsonify({"error": "Missing signature"}), 401

    try:
        verify_signature(
            payload=raw_body,
            signature=signature,
            secret=WEBHOOK_SECRET,
            timestamp=timestamp,
        )
    except WebhookVerificationError:
        logger.warning("Webhook signature verification failed")
        return jsonify({"error": "Invalid signature"}), 401

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = payload.get("event")
    logger.info(f"Received webhook event: {event_type}")

    if event_type != "message.received":
        return jsonify({"status": "ignored"}), 200

    data      = payload.get("data", {})
    message   = data.get("message", {})
    thread_id = data.get("thread_id", "")
    inbox_id  = data.get("inbox_id", "")
    subject   = data.get("subject", "(no subject)")
    sender    = message.get("from", "")
    body      = message.get("text") or message.get("html", "")

    if not sender or not body:
        logger.warning("Webhook payload missing sender or body — skipping")
        return jsonify({"status": "skipped"}), 200

    logger.info(f"Queuing reply for {sender} on thread {thread_id}")

    # Offload LLM + send to a background thread — webhook returns immediately
    threading.Thread(
        target=generate_and_send_reply,
        args=(sender, subject, body, thread_id, inbox_id),
        daemon=True,
    ).start()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
