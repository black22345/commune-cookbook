# ADR-005: Synchronous vs. Asynchronous Client Selection

**Status:** Accepted
**Date:** 2026-03-01
**Technical Area:** SDK Client Architecture

---

## Context

The commune-python SDK ships two client classes: `CommuneClient` (synchronous, backed by `httpx.Client`) and `AsyncCommuneClient` (asynchronous, backed by `httpx.AsyncClient`). Both expose the same surface area — `client.inboxes`, `client.messages`, `client.domains`, etc. — but their calling conventions are incompatible at the Python runtime level.

Python's asyncio model imposes a hard constraint: an `async def` function returns a coroutine object when called without `await`. That object is not the result — it is an unevaluated computation. If code in a synchronous context calls `client.messages.send(...)` on an `AsyncCommuneClient`, it receives a coroutine, not a `SendMessageResult`. The code appears to succeed: no exception is raised at the call site. The failure surfaces later, when the caller attempts to access an attribute on the coroutine (e.g., `result.message_id`), yielding `AttributeError: 'coroutine' object has no attribute 'message_id'`. In some code paths the coroutine is never awaited and is silently garbage-collected, meaning the API call never happened — no exception, no send, no visible error.

The inverse failure also occurs: `CommuneClient` (sync) used inside an `async def` handler. Because `CommuneClient` uses a blocking HTTP client internally, calling it from an async context blocks the event loop for the duration of the network call — typically 100–800ms per request. Under concurrent load, this serializes all requests through the blocking call, eliminating the concurrency benefit of the async framework and potentially starving the event loop's other tasks (timers, background coroutines, heartbeats).

The affected runtime environments break down as follows:

**Synchronous runtimes** (must use `CommuneClient`):
- Flask routes (`def view_func()`)
- Django views without ASGI adapter
- AWS Lambda handlers (`def handler(event, context)`)
- Celery tasks with the default prefork pool
- OpenAI `@function_tool` decorated functions (synchronous by SDK design)
- LangChain `Tool` callables
- Standard `pytest` test functions

**Asynchronous runtimes** (must use `AsyncCommuneClient`):
- FastAPI route handlers (`async def endpoint()`)
- Starlette request handlers
- async Celery workers using gevent or asyncio pool
- LangGraph node functions declared `async def`
- `async def` agent tool functions in any framework
- `pytest-asyncio` test functions

The failure mode is asymmetric in detectability. Using `AsyncCommuneClient` in a sync context produces a visible `AttributeError` at attribute access time — usually caught in testing. Using `CommuneClient` in an async context produces no error: the code works, but measurably degrades throughput. In a FastAPI service handling 50 concurrent requests, a single blocking `CommuneClient` call per request can reduce effective QPS by 40–60% under load.

A secondary concern arises in middleware or utility code — functions called from both sync and async contexts. A helper like `def send_confirmation(user_id, event)` cannot hold a global `CommuneClient` and be called from a FastAPI handler without blocking the event loop.

---

## Decision

**Rule:** If your handler is `def`, use `CommuneClient`. If your handler is `async def`, use `AsyncCommuneClient`. Never mix within the same call stack.

Client instances should be created at module load time (or application startup), not per-request, and stored as module-level or application-state singletons. The pattern is:

```python
# sync_app.py (Flask, Lambda, Celery)
from commune import CommuneClient

client = CommuneClient(api_key=os.environ["COMMUNE_API_KEY"])

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    payload = client.webhooks.verify(request.get_data(), request.headers)
    # ... sync processing
```

```python
# async_app.py (FastAPI, Starlette)
from commune import AsyncCommuneClient
from contextlib import asynccontextmanager

client: AsyncCommuneClient

@asynccontextmanager
async def lifespan(app):
    global client
    client = AsyncCommuneClient(api_key=os.environ["COMMUNE_API_KEY"])
    yield
    await client.close()

@app.post("/webhook")
async def handle_webhook(request: Request):
    payload = await client.webhooks.verify(await request.body(), request.headers)
    # ... async processing
```

For middleware or utility functions that must operate in both contexts, accept the client as a parameter with a union type hint:

```python
from commune import CommuneClient, AsyncCommuneClient
from typing import Union

CommuneClientType = Union[CommuneClient, AsyncCommuneClient]

def build_reply_body(thread_context: dict) -> str:
    """Pure function — no client needed. Keep I/O out of pure helpers."""
    ...

# The async send is the caller's responsibility, not the helper's
```

---

## Alternatives Considered

**1. Single auto-detecting client that dispatches based on running event loop**

Could check `asyncio.get_running_loop()` at call time and execute either `httpx.Client.request()` or `httpx.AsyncClient.request()` accordingly. This is technically feasible but rejected for three reasons:

- It makes the blocking vs. non-blocking behavior invisible at the call site — a reader cannot determine whether a line of code blocks without tracing the runtime context
- It requires either two underlying HTTP clients instantiated on every call or complex lazy initialization
- It creates subtle ordering bugs: if a sync method call happens to run when a loop is available (e.g., from a sync function called from a Celery task that shares process memory with an asyncio context), it silently switches to async behavior

**2. Always-async client with `asyncio.run()` shim for sync callers**

Expose only `AsyncCommuneClient` and provide a sync wrapper that calls `asyncio.run()`. This fails in two common scenarios:

- Jupyter notebooks use a running event loop (`IPython` installs one at startup). Calling `asyncio.run()` from within a running loop raises `RuntimeError: This event loop is already running`. The `nest_asyncio` workaround exists but is a patch that changes asyncio semantics globally
- Some test frameworks (pytest with async plugins, or frameworks that instrument the event loop) have the same issue

**3. Always-synchronous, no async client**

Simplest API surface. Rejected because FastAPI/Starlette are dominant patterns for agent microservices: they are built around asyncio, and blocking the event loop in them is measurably harmful at production traffic levels (see Context section above). Forcing users to use a blocking client in an async framework is bad enough to warrant maintaining two clients.

---

## Consequences

**Positive:**
- The `def`/`async def` rule is a single, memorable decision point that requires no understanding of asyncio internals
- The coroutine-not-awaited silent failure class is eliminated entirely — if you follow the rule, you never accidentally get a coroutine where a result was expected
- Client lifecycle management (connection pooling, keep-alive) works correctly in both contexts

**Negative:**
- Two clients to maintain in the SDK. They share an HTTP layer and response model, but the async client requires `async with`, `await client.close()`, and async context managers throughout — divergence is an ongoing maintenance surface
- Middleware and utility functions called from both sync and async contexts must avoid holding client state, or must accept the client as a parameter, slightly complicating shared-code design
- Migration from a synchronous framework (Flask) to an asynchronous one (FastAPI) requires replacing every `CommuneClient` instantiation with `AsyncCommuneClient` and adding `await` at every call site — not automatic, not incremental

---

## Related

- **ADR-008**: Background processing for webhook handlers. Async webhook handlers (FastAPI) that enqueue work to async task queues use `AsyncCommuneClient`; sync background workers (Celery prefork) use `CommuneClient`. The correct client flows from the worker context, not the framework that received the webhook.
