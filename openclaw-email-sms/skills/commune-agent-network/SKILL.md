# Skill: commune-agent-network

> Every OpenClaw agent can become a node in an agent network. Each agent gets a permanent email address. Agents send tasks to each other, receive results as email replies, and maintain full thread history — without shared state or coordination infrastructure.

## Authentication

Requires `COMMUNE_API_KEY` environment variable. Get one at commune.email.

```
Base URL: https://api.commune.email
Auth header: Authorization: Bearer $COMMUNE_API_KEY
```

---

## Core Concept

```
You (WhatsApp)  →  OpenClaw Agent (orchestrator@commune.email)
                         ↓  sends task email
               Researcher Agent (researcher@commune.email)
                         ↓  replies with result
               OpenClaw Agent reads full thread
                         ↓
               Reports result back to you
```

Each agent has one inbox address. Addresses are permanent. The email thread preserves the full task chain.

---

## Operations

### Provision a worker agent inbox

```
POST /v1/inboxes
Body: { "localPart": "researcher" }
Response: { "id": "inbox_xxx", "address": "researcher@org.commune.email" }
```

Save the `id` as `COMMUNE_WORKER_INBOX_ID` and `address` as `COMMUNE_WORKER_ADDRESS`.

---

### Send a task to another agent

```
POST /v1/messages/send
Body: {
  "to": "researcher@org.commune.email",
  "subject": "Research task: cloud database pricing",
  "text": "Please compare pricing for Neon, Supabase, and Railway Postgres. Return as a comparison table.",
  "inboxId": "COMMUNE_ORCHESTRATOR_INBOX_ID",
  "idempotencyKey": "research-db-pricing-2026"
}
Response: { "thread_id": "thread_xxx" }
```

Save `thread_id`. It is the unique identifier for this task. The idempotency key ensures the task is sent exactly once even if retried.

---

### Read the full task chain

```
GET /v1/threads/THREAD_ID/messages
```

Returns every message in the thread in order:
- `direction: "outbound"` = sent by your agent
- `direction: "inbound"` = received from the worker

---

### Reply in a task thread (worker side)

```
POST /v1/messages/send
Body: {
  "to": "orchestrator@org.commune.email",
  "subject": "Re: Research task: cloud database pricing",
  "text": "Here is the comparison: ...",
  "inboxId": "COMMUNE_WORKER_INBOX_ID",
  "thread_id": "thread_xxx"
}
```

`thread_id` binds the reply to the original task. Without it, a new thread is created.

---

### Search for past tasks (semantic deduplication)

```
GET /v1/search/threads?q=TASK_DESCRIPTION&inbox_id=COMMUNE_WORKER_INBOX_ID&limit=3
```

Returns past tasks ranked by semantic similarity. If a similar task was already completed, the worker can return the cached result instead of repeating work.

---

## Example Prompts for Your OpenClaw Agent

- "Ask the researcher agent to compare Postgres pricing and report back"
- "Send a task to analyst@myteam.commune.email to summarize the Q1 sales threads"
- "Check if researcher replied to the database task"
- "Read the full thread for task thread_xxx"
- "What tasks has the researcher agent completed this week?"

---

## Agent Mesh Pattern

```
orchestrator@org.commune.email
  ↓ send task
researcher@org.commune.email
  ↓ reply in thread

orchestrator reads full chain:
  GET /v1/threads/thread_xxx/messages
  → [task, result]
```

For multi-step pipelines, pass the `thread_id` from stage to stage. Each agent can `GET /v1/threads/:id/messages` to see the full history before processing.

---

## Shell Helper

```bash
node ~/.openclaw/workspace/skills/commune-agent-network/commune-network.js <command> [args]
```

| Command | Args | What it does |
|---------|------|--------------|
| `send-task` | `to subject body` | Sends task email to agent address, returns thread_id |
| `read-thread` | `thread_id` | Prints full task→result chain |
| `reply-in-thread` | `thread_id to body` | Replies in existing task thread |
| `search-past-tasks` | `query` | Semantic search for similar past tasks |
| `list-worker-threads` | `[inbox_id]` | Lists threads waiting for reply |

---

## Installation

```bash
cp -r skills/commune-agent-network ~/.openclaw/workspace/skills/
```

Then add to your OpenClaw agent configuration:
```yaml
skills:
  - commune-agent-network
```

Set environment variables:
```bash
COMMUNE_API_KEY=comm_...
COMMUNE_ORCHESTRATOR_INBOX_ID=inbox_...
```
