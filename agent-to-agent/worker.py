"""
Agent-to-Agent: Worker (Researcher)

Webhook handler for the researcher agent. Receives typed task emails from
the orchestrator, checks past work via semantic search, runs the LLM, and
replies in the same thread.

Install:
    pip install commune-mail openai flask

Usage:
    export COMMUNE_API_KEY=comm_...
    export OPENAI_API_KEY=sk-...
    export COMMUNE_WEBHOOK_SECRET=whsec_...
    export COMMUNE_RESEARCHER_INBOX_ID=inbox_...
    flask --app worker run --port 8001

Expose with ngrok or similar, then register the webhook URL in your
Commune dashboard for the researcher inbox.
"""
import os, json
from flask import Flask, request, jsonify
from commune import CommuneClient, verify_commune_webhook
from openai import OpenAI

commune = CommuneClient(api_key=os.environ["COMMUNE_API_KEY"])
openai  = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

INBOX_ID       = os.environ["COMMUNE_RESEARCHER_INBOX_ID"]
WEBHOOK_SECRET = os.environ["COMMUNE_WEBHOOK_SECRET"]

app = Flask(__name__)


@app.post("/webhook/commune")
def handle_task():
    # ── Verify the payload is from Commune ───────────────────────────────────
    verify_commune_webhook(
        raw_body=request.get_data(),
        timestamp=request.headers["x-commune-timestamp"],
        signature=request.headers["x-commune-signature"],
        secret=WEBHOOK_SECRET,
    )

    payload = request.json

    # Only process inbound emails (not delivery events)
    if payload.get("event") != "message.received":
        return jsonify({"ok": True})

    thread_id    = payload["thread_id"]
    sender_email = payload["sender"]
    subject      = payload.get("subject", "Re: task")

    # ── Structured task fields (auto-extracted by Commune) ───────────────────
    # Because we configured an extraction_schema on this inbox in orchestrator.py,
    # Commune has already parsed the email into typed fields — no parsing here.
    task = payload.get("extracted") or {}
    query         = task.get("query") or payload.get("content", "")
    output_format = task.get("output_format", "prose")
    max_words     = task.get("max_words", 300)

    # ── Check for relevant past work ─────────────────────────────────────────
    # Semantic search across this agent's full task history.
    # If a similar task was completed before, include it as context.
    past_threads = commune.search.threads(
        query=query,
        inbox_id=INBOX_ID,
        limit=3,
    )
    past_context = ""
    for pt in past_threads:
        if pt.thread_id != thread_id:          # skip the current thread
            msgs = commune.threads.messages(pt.thread_id)
            # Look for a previous result (outbound reply from this worker)
            prior_results = [m for m in msgs if m.direction == "outbound"]
            if prior_results:
                past_context += f"\nPrevious research on '{pt.subject}':\n{prior_results[-1].content[:400]}\n"

    # ── Run the LLM ──────────────────────────────────────────────────────────
    system_prompt = (
        f"You are a research agent. Respond in {output_format} format. "
        f"Stay under {max_words} words. Be factual and concise."
    )
    user_prompt = query
    if past_context:
        user_prompt += f"\n\nContext from past research:\n{past_context}"

    completion = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
    )
    result = completion.choices[0].message.content

    # ── Reply in the same thread ──────────────────────────────────────────────
    # thread_id binds this reply to the original task.
    # The orchestrator reads the full chain via commune.threads.messages(thread_id).
    commune.messages.send(
        to=sender_email,
        subject=f"Re: {subject}",
        text=result,
        inbox_id=INBOX_ID,
        thread_id=thread_id,
    )

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(port=8001, debug=True)
