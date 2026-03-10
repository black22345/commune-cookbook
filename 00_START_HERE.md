# Commune Cookbook — Start Here

This repo contains code patterns, production examples, and notebooks for building AI agents with email and SMS using [Commune](https://commune.email).

---

## Ecosystem map

Three repos, three roles:

| Repo | What it is | Install |
|------|-----------|---------|
| **[commune-python](https://github.com/shanjai-raj/commune-python)** | Python SDK — the core library | `pip install commune-mail` |
| **[commune-ai](https://github.com/shanjai-raj/commune-ai)** | TypeScript/Node SDK | `npm install commune-ai` |
| **[commune-mcp](https://github.com/shanjai-raj/commune-mcp)** | MCP server for Claude Desktop / Cursor / Windsurf | `npx @smithery/cli install commune` |
| **commune-cookbook** ← you are here | Examples, notebooks, patterns | — |

Start with the SDK for your language. Come here for examples. Go to `commune-mcp` if you want Claude Desktop or Cursor to have email.

---

## Choose your path

**→ I want to run something in 5 minutes**
Open [`notebooks/01_quickstart.ipynb`](notebooks/01_quickstart.ipynb) in Colab. No local setup.

**→ I want a working agent for my framework**

| Framework | Start here |
|-----------|-----------|
| OpenClaw | [`openclaw-email-sms/`](openclaw-email-sms/) — `clawhub install shanjairaj7/commune` |
| LangChain | [`langchain/customer-support/`](langchain/customer-support/) |
| CrewAI | [`crewai/support-crew/`](crewai/support-crew/) |
| OpenAI Agents SDK | [`openai-agents/support-agent/`](openai-agents/support-agent/) |
| LangGraph | [`notebooks/06_langgraph_email_agent.ipynb`](notebooks/06_langgraph_email_agent.ipynb) |
| Claude tool_use | [`claude/support-agent/`](claude/support-agent/) |
| TypeScript / Express | [`typescript/webhook-handler/`](typescript/webhook-handler/) |
| MCP (Claude Desktop) | [`mcp-server/`](mcp-server/) |
| Agent-to-agent (any framework) | [`agent-to-agent/`](agent-to-agent/) |

**→ I want to build a specific thing**

| Goal | Go to |
|------|-------|
| Customer support inbox | [`use-cases/customer-support/`](use-cases/customer-support/) |
| Hiring / recruiting pipeline | [`use-cases/hiring-and-recruiting/`](use-cases/hiring-and-recruiting/) |
| Sales outreach | [`use-cases/sales-and-marketing/`](use-cases/sales-and-marketing/) |
| Incident alerts | [`use-cases/notifications-and-alerts/`](use-cases/notifications-and-alerts/) |
| SMS + email combined | [`notebooks/08_sms_email_combined.ipynb`](notebooks/08_sms_email_combined.ipynb) |
| Multi-agent coordination | [`typescript/multi-agent/`](typescript/multi-agent/) |
| Agent-to-agent communication | [`agent-to-agent/`](agent-to-agent/) |
| OpenClaw personal agent | [`openclaw-email-sms/`](openclaw-email-sms/) |

**→ I want to understand how things work**

Read the capabilities in order — each one is short and standalone:

1. [`capabilities/quickstart/`](capabilities/quickstart/) — Create an inbox, send an email (5 min)
2. [`capabilities/email-threading/`](capabilities/email-threading/) — Reply in-thread (RFC 5322)
3. [`capabilities/structured-extraction/`](capabilities/structured-extraction/) — Auto-parse email fields to JSON
4. [`capabilities/webhook-delivery/`](capabilities/webhook-delivery/) — Receive real-time inbound events
5. [`capabilities/semantic-search/`](capabilities/semantic-search/) — Search across thread history
6. [`capabilities/sms/`](capabilities/sms/) — SMS quickstart, two-way, broadcast

**→ I hit a bug or something isn't working**

1. Check [`ANTIPATTERNS.md`](ANTIPATTERNS.md) — documents the 8 most common failure modes
2. Check [`commune-python/ERRORS.md`](https://github.com/shanjai-raj/commune-python/blob/main/ERRORS.md) — error codes and fixes
3. Check the relevant ADR — architecture decisions that explain *why* things are built the way they are

**→ I want to understand the architecture decisions**

[`adr/`](adr/) — 8 architecture decision records, each one explains a non-obvious design choice:

| ADR | Decision |
|-----|---------|
| [001](adr/001-use-thread-id-for-all-replies.md) | Always pass `thread_id` when replying |
| [002](adr/002-verify-webhook-signatures-before-parse.md) | Capture raw bytes before JSON parsing |
| [003](adr/003-extraction-schemas-over-llm-parsing.md) | Use per-inbox schemas instead of LLM parsing |
| [004](adr/004-one-inbox-per-agent-identity.md) | One inbox per agent for isolation |
| [005](adr/005-sync-vs-async-client-selection.md) | Sync vs async client selection |
| [006](adr/006-idempotency-keys-for-agent-email-sends.md) | Idempotency keys to prevent duplicate sends |
| [007](adr/007-prompt-injection-defense-at-webhook-boundary.md) | Prompt injection defense at the boundary |
| [008](adr/008-background-processing-for-webhook-handlers.md) | Background processing for webhook handlers |

---

## Notebooks curriculum

The notebooks are numbered — read them in order for a progressive curriculum, or jump to the one you need:

| # | Notebook | What you learn |
|---|---------|---------------|
| 01 | [`01_quickstart.ipynb`](notebooks/01_quickstart.ipynb) | Create inbox, send, read threads |
| 02 | [`02_langchain_customer_support.ipynb`](notebooks/02_langchain_customer_support.ipynb) | LangChain support agent |
| 03 | [`03_structured_extraction.ipynb`](notebooks/03_structured_extraction.ipynb) | Auto-parse email fields |
| 04 | [`04_crewai_multi_agent.ipynb`](notebooks/04_crewai_multi_agent.ipynb) | Multi-agent email coordination |
| 05 | [`05_openai_agents_email.ipynb`](notebooks/05_openai_agents_email.ipynb) | OpenAI Agents SDK tools |
| 06 | [`06_langgraph_email_agent.ipynb`](notebooks/06_langgraph_email_agent.ipynb) | LangGraph stateful email agent |
| 07 | [`07_async_streaming.ipynb`](notebooks/07_async_streaming.ipynb) | Async patterns and streaming |
| 08 | [`08_sms_email_combined.ipynb`](notebooks/08_sms_email_combined.ipynb) | SMS + email combined agent |
| 09 | [`09_langchain_production.ipynb`](notebooks/09_langchain_production.ipynb) | LangChain production patterns |
| 10 | [`10_crewai_production.ipynb`](notebooks/10_crewai_production.ipynb) | CrewAI production patterns |
| 11 | [`11_openai_agents_production.ipynb`](notebooks/11_openai_agents_production.ipynb) | OpenAI Agents production patterns |
| 12 | [`12_claude_tool_use.ipynb`](notebooks/12_claude_tool_use.ipynb) | Claude tool_use + autogen |

---

## Repo layout

```
commune-cookbook/
├── 00_START_HERE.md              ← you are here
│
├── capabilities/                 ← Feature-by-feature learning path (start here for new users)
│   ├── quickstart/               ← 1. Create inbox, send email (5 min)
│   ├── email-threading/          ← 2. Reply in-thread
│   ├── structured-extraction/    ← 3. Auto-parse email fields
│   ├── webhook-delivery/         ← 4. Receive real-time events
│   ├── semantic-search/          ← 5. Search across threads
│   └── sms/                      ← 6. SMS patterns
│
├── notebooks/                    ← Interactive notebooks (numbered curriculum)
│   ├── 01_quickstart.ipynb
│   ├── 02_langchain_customer_support.ipynb
│   └── ...
│
├── use-cases/                    ← Production patterns by domain
│   ├── customer-support/
│   ├── hiring-and-recruiting/
│   ├── sales-and-marketing/
│   └── notifications-and-alerts/
│
├── langchain/                    ← LangChain examples
├── crewai/                       ← CrewAI examples
├── openai-agents/                ← OpenAI Agents SDK examples
├── claude/                       ← Claude tool_use examples
├── typescript/                   ← TypeScript / Node examples
├── sms/                          ← SMS-focused examples
├── mcp-server/                   ← MCP server example
│
├── adr/                          ← Architecture decisions (why things are built this way)
├── ANTIPATTERNS.md               ← Common failure modes and fixes
├── COMPARISON.md                 ← Commune vs Gmail API vs SendGrid vs others
└── README.md                     ← Full feature and framework overview
```

---

## Get an API key

[commune.email →](https://commune.email)

Keys start with `comm_`. Set as `COMMUNE_API_KEY` in your environment or `.env` file.
