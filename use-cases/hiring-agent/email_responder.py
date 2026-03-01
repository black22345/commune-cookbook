"""
Hiring agent email responder — LangChain-based candidate communication.

Receives inbound email events for a hiring inbox, uses an LLM to decide
whether to send a screening questionnaire, schedule an interview, or
reject the candidate, then sends the appropriate reply.

Install:
    pip install flask langchain langchain-openai commune-mail

Usage:
    export COMMUNE_API_KEY=comm_...
    export OPENAI_API_KEY=sk-...
    export COMMUNE_WEBHOOK_SECRET=whsec_...
    export COMMUNE_INBOX_ID=i_...
    python email_responder.py
"""

import json
import logging
import os

from flask import Flask, jsonify, request
from langchain.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from commune import CommuneClient
from commune.webhooks import verify_signature, WebhookVerificationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

WEBHOOK_SECRET  = os.environ["COMMUNE_WEBHOOK_SECRET"]
COMMUNE_API_KEY = os.environ["COMMUNE_API_KEY"]
INBOX_ID        = os.environ["COMMUNE_INBOX_ID"]

commune = CommuneClient(api_key=COMMUNE_API_KEY)

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

TRIAGE_PROMPT = ChatPromptTemplate.from_template("""
You are a hiring assistant at a fast-growing tech startup.

An email arrived from a job applicant. Decide what to do:
- "screen": The candidate looks promising — send them 3 screening questions
- "schedule": The candidate passed screening — offer them 3 interview slots
- "reject": The candidate is not a fit — send a polite rejection

Email subject: {subject}
Email body: {body}

Reply with only one of: screen, schedule, reject
""")

SCREEN_REPLY = """Hi {name},

Thanks for your interest in the {role} position! Your background looks interesting.

To move forward, please answer these 3 quick questions:
1. What's the most complex technical system you've built? Describe the architecture.
2. How do you approach debugging a production issue you've never seen before?
3. What's a time you had to make a technical decision with incomplete information?

Reply to this email with your answers — looking forward to hearing from you!

Best,
The Hiring Team
"""

REJECT_REPLY = """Hi {name},

Thank you for your interest in joining us. After careful consideration, we've decided
to move forward with other candidates whose experience more closely matches our
current needs.

We appreciate the time you took to reach out and wish you the best in your search.

Best regards,
The Hiring Team
"""


def get_candidate_history(inbox_id: str, sender: str) -> list[dict]:
    """
    Load all previous emails with this candidate for LLM context.

    Scoped to the hiring inbox via inbox_id — prevents cross-inbox data
    leakage and keeps the LLM context clean.
    """
    messages = commune.messages.list(inbox_id=inbox_id, sender=sender)
    history = []
    for msg in messages:
        role = "user" if msg.direction == "inbound" else "assistant"
        history.append({"role": role, "content": msg.content})
    return history


def triage_candidate(subject: str, body: str) -> str:
    """Use LLM to decide whether to screen, schedule, or reject."""
    chain = TRIAGE_PROMPT | llm
    result = chain.invoke({"subject": subject, "body": body})
    return result.content.strip().lower()


@app.route("/webhook/hiring", methods=["POST"])
def handle_hiring_email():
    """
    Webhook handler for the hiring inbox.

    Verifies the Commune signature, triages the candidate email,
    and sends the appropriate reply in the same thread.
    """
    raw_body = request.get_data()
    signature = request.headers.get("X-Commune-Signature", "")
    timestamp = request.headers.get("X-Commune-Timestamp", "")

    try:
        verify_signature(
            payload=raw_body,
            signature=signature,
            secret=WEBHOOK_SECRET,
            timestamp=timestamp,
        )
    except WebhookVerificationError:
        logger.warning("Invalid webhook signature")
        return jsonify({"error": "Invalid signature"}), 401

    payload = json.loads(raw_body)

    if payload.get("event") != "message.received":
        return jsonify({"status": "ignored"}), 200

    data      = payload.get("data", {})
    thread_id = data.get("thread_id", "")
    inbox_id  = data.get("inbox_id") or INBOX_ID
    subject   = data.get("subject", "(no subject)")
    sender    = data.get("sender", "")
    body_text = data.get("text", "")

    if not sender or not body_text:
        return jsonify({"status": "skipped"}), 200

    logger.info(f"Hiring email from {sender}: {subject}")

    candidate_name = sender.split("@")[0].capitalize()
    role = "Software Engineer"  # would come from inbox metadata in a real system

    decision = triage_candidate(subject, body_text)
    logger.info(f"Triage decision for {sender}: {decision}")

    if decision == "screen":
        reply_body = SCREEN_REPLY.format(name=candidate_name, role=role)
        reply_subject = f"Re: {subject}"
    elif decision == "schedule":
        reply_body = (
            f"Hi {candidate_name},\n\nGreat answers! Let's schedule a call.\n\n"
            f"Are any of these times good for you?\n"
            f"- Tuesday 2pm EST\n- Wednesday 10am EST\n- Thursday 3pm EST\n\n"
            f"Looking forward to meeting you!\n\nBest,\nThe Hiring Team"
        )
        reply_subject = f"Re: {subject}"
    else:
        reply_body = REJECT_REPLY.format(name=candidate_name)
        reply_subject = f"Re: {subject}"

    commune.messages.send(
        to=sender,
        subject=reply_subject,
        text=reply_body,
        inbox_id=inbox_id,
        thread_id=thread_id,   # continues the candidate's existing thread
    )

    logger.info(f"Reply sent to {sender} (decision: {decision})")
    return jsonify({"status": "ok", "decision": decision}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    logger.info(f"Starting hiring agent webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
