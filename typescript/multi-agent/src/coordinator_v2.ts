/**
 * Multi-Agent Coordinator v2 — TypeScript
 *
 * Extended version of the multi-agent coordinator with:
 *   - In-flight deduplication to prevent double-processing on webhook retries
 *   - Cross-agent thread context loading for richer specialist replies
 *   - Structured task dispatch via email
 *
 * Architecture:
 *   1. Customer email arrives at the orchestrator inbox
 *   2. Coordinator deduplicates, classifies intent, assigns to specialist
 *   3. Specialist loads full thread history and replies in the customer's thread
 *
 * Environment:
 *   COMMUNE_API_KEY              — from commune.email dashboard
 *   COMMUNE_WEBHOOK_SECRET       — for signature verification
 *   OPENAI_API_KEY               — for classification
 *   SPECIALIST_BILLING_INBOX     — billing agent inbox address
 *   SPECIALIST_TECHNICAL_INBOX   — technical agent inbox address
 *   ORCHESTRATOR_INBOX_ID        — inboxId of the orchestrator inbox
 *
 * Install:
 *   npm install
 *
 * Usage:
 *   npm run dev
 */
import express, { type Request, type Response } from 'express';
import { CommuneClient, verifyCommuneWebhook, type InboundEmailWebhookPayload } from 'commune-ai';
import OpenAI from 'openai';

const app = express();
const port = process.env.PORT || 3000;

const commune = new CommuneClient({ apiKey: process.env.COMMUNE_API_KEY! });
const openai  = new OpenAI({ apiKey: process.env.OPENAI_API_KEY! });

// FIX BUG-CORRECT-1: replaced in-process Set with Redis-backed SETNX deduplication.
// An in-process Set fails in multi-worker deployments (PM2, Railway, Heroku) because
// each worker maintains its own copy — the same messageId would be processed once per
// worker. Redis SETNX is atomic, cross-process, and survives restarts/redeploys.
//
// For a minimal Redis client: npm install ioredis
// For production, use a managed Redis (Railway Redis, Upstash, etc.)
import Redis from 'ioredis';

const redis = new Redis(process.env.REDIS_URL ?? 'redis://localhost:6379');
const DEDUP_TTL_SECONDS = 300; // 5 minutes — covers Commune's retry window

async function isDuplicate(messageId: string): Promise<boolean> {
  const key = `commune:processed:${messageId}`;
  // SETNX + EXPIRE is atomic via SET NX EX — returns null if key already exists
  const result = await redis.set(key, '1', 'EX', DEDUP_TTL_SECONDS, 'NX');
  return result === null; // null means key existed — duplicate
}

// ── Orchestrator webhook ───────────────────────────────────────────────────

app.use('/webhook/orchestrator', express.raw({ type: 'application/json' }));

app.post('/webhook/orchestrator', async (req: Request, res: Response) => {
  // Verify signature against raw bytes
  const rawBody = req.body as Buffer;
  try {
    verifyCommuneWebhook({
      rawBody:   rawBody.toString('utf8'),
      timestamp: req.headers['x-commune-timestamp'] as string,
      signature: req.headers['x-commune-signature'] as string,
      secret:    process.env.COMMUNE_WEBHOOK_SECRET!,
    });
  } catch {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const payload: InboundEmailWebhookPayload = JSON.parse(rawBody.toString('utf8'));
  const { message } = payload;

  if (message.direction !== 'inbound') {
    return res.status(200).json({ ok: true });
  }

  // FIX BUG-CORRECT-1: Redis-backed deduplication — safe across workers and restarts
  if (await isDuplicate(message.id)) {
    console.log(`[Orchestrator] Duplicate message ${message.id} — skipping`);
    return res.status(200).json({ ok: true, duplicate: true });
  }

  // Acknowledge immediately
  res.status(200).json({ ok: true });

  const sender = message.participants.find(p => p.role === 'sender')?.identity;
  if (!sender) return;

  console.log(`\n[Orchestrator] Message ${message.id} from ${sender}`);

  try {
    const intent = await classifyIntent(openai, message.content ?? '');
    console.log(`  Classified: ${intent}`);

    const specialistInbox =
      intent === 'billing'
        ? process.env.SPECIALIST_BILLING_INBOX!
        : process.env.SPECIALIST_TECHNICAL_INBOX!;

    await commune.messages.send({
      to:      specialistInbox,
      subject: `[Task:${intent}] ${message.metadata?.subject ?? ''}`,
      text: JSON.stringify({
        userEmail:        sender,
        userInboxId:      payload.inboxId,
        originalThreadId: message.threadId,
        originalSubject:  message.metadata?.subject ?? '',
        intent,
      }),
      inboxId: payload.inboxId,
      threadId: message.threadId,
    });

    await commune.threads.setStatus(message.threadId, 'waiting');
    await commune.threads.addTags(message.threadId, [intent]);

    console.log(`  Dispatched to ${intent} specialist`);
  } catch (err) {
    console.error('[Orchestrator] Error:', err);
  }
});

// ── Specialist webhook ─────────────────────────────────────────────────────

app.use('/webhook/specialist', express.raw({ type: 'application/json' }));

app.post('/webhook/specialist', async (req: Request, res: Response) => {
  const rawBody = req.body as Buffer;
  try {
    verifyCommuneWebhook({
      rawBody:   rawBody.toString('utf8'),
      timestamp: req.headers['x-commune-timestamp'] as string,
      signature: req.headers['x-commune-signature'] as string,
      secret:    process.env.COMMUNE_WEBHOOK_SECRET!,
    });
  } catch {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const payload: InboundEmailWebhookPayload = JSON.parse(rawBody.toString('utf8'));
  const { message } = payload;

  if (message.direction !== 'inbound') {
    return res.status(200).json({ ok: true });
  }

  res.status(200).json({ ok: true });

  try {
    const task = JSON.parse(message.content ?? '{}') as {
      userEmail:        string;
      userInboxId:      string;
      originalThreadId: string;
      originalSubject:  string;
      intent:           string;
    };

    console.log(`\n[Specialist] Task for ${task.userEmail} — intent: ${task.intent}`);

    // FIX BUG-CORRECT-2: pass inboxId to scope the thread lookup to the customer's
    // inbox. Without it the API may return messages from any inbox sharing that
    // thread ID, or fail with a permissions error in multi-tenant configurations.
    const threadMessages = await commune.threads.messages(task.originalThreadId, {
      inboxId: task.userInboxId,  // FIX: scope to customer's inbox — prevents cross-tenant read
    });

    const history = threadMessages.map(m => ({
      role:    m.direction === 'inbound' ? 'user' as const : 'assistant' as const,
      content: m.content ?? '',
    }));

    // FIX BUG-SEC-1: do NOT interpolate raw email content (message.content) into
    // the system prompt — that enables indirect prompt injection. An attacker can
    // embed "Ignore all prior instructions" in an email body and the LLM will see
    // it as a system-level directive. Instead, use only the typed, structured fields
    // (intent, originalSubject) that were classified by the orchestrator.
    const systemPrompt = `You are a ${task.intent} support specialist.
The customer is writing about: "${task.originalSubject}"
Reply professionally and resolve the customer's issue. Sign off as "Support Team".`;

    const completion = await openai.chat.completions.create({
      model: 'gpt-4o-mini',
      messages: [
        { role: 'system', content: systemPrompt },
        ...history,
      ],
    });

    const reply = completion.choices[0].message.content!;

    await commune.messages.send({
      to:       task.userEmail,
      subject:  `Re: ${task.originalSubject}`,
      text:     reply,
      inboxId:  task.userInboxId,
      threadId: task.originalThreadId,
    });

    await commune.threads.setStatus(task.originalThreadId, 'closed');
    console.log(`  Reply sent, thread closed`);

  } catch (err) {
    console.error('[Specialist] Error:', err);
  }
});

// ── Health check ───────────────────────────────────────────────────────────

app.get('/health', (_: Request, res: Response) => res.json({ ok: true, version: 'coordinator-v2' }));

// ── Start ──────────────────────────────────────────────────────────────────

app.listen(port, () => {
  console.log(`Multi-agent coordinator v2 running on port ${port}`);
  console.log(`  POST /webhook/orchestrator — receives customer emails`);
  console.log(`  POST /webhook/specialist   — receives dispatched tasks`);
  console.log(`  GET  /health               — health check`);
});

// ── Helpers ────────────────────────────────────────────────────────────────

async function classifyIntent(
  client: OpenAI,
  content: string,
): Promise<'billing' | 'technical' | 'general'> {
  const completion = await client.chat.completions.create({
    model: 'gpt-4o-mini',
    response_format: { type: 'json_object' },
    messages: [
      {
        role: 'system',
        content: `Classify the email intent. Return JSON: {"intent": "billing"|"technical"|"general"}`,
      },
      { role: 'user', content },
    ],
  });
  const result = JSON.parse(completion.choices[0].message.content!);
  return result.intent as 'billing' | 'technical' | 'general';
}
