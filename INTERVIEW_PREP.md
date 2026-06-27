# Interview Prep — Unified Model Router

---

## How to Explain This Project in 2 Minutes

> *"I built a production-ready API gateway that acts as a unified router for Large Language Model providers — similar to OpenRouter. The core idea is: instead of your application talking directly to OpenAI, Anthropic, or Gemini with different API formats, it talks to one single endpoint on my router. The router handles all the provider-specific translation, live streaming, and automatic fallbacks.*
>
> *There are three main features. First, schema translation — the router accepts one standard OpenAI-compatible format and translates it to whatever each provider actually expects, and translates the response back. Second, live SSE streaming — tokens are piped from the provider to the client as they arrive, word by word, with zero buffering. Third, resilient fallback — if the primary provider returns a 503 or times out, the router silently tries the next provider in the chain. The client sees a successful response and never knows there was a failure.*
>
> *I also built a circuit breaker so if a provider is consistently failing, the router stops wasting time trying it and goes straight to the fallback. The whole thing is tested with 127 tests, structured JSON audit logging, and a GitHub Actions CI pipeline."*

---

## Core Concepts to Be Fluent On

### 1. What is an Adapter Pattern?
Each provider (DigitalOcean, OpenAI, Anthropic) has different API field names, URL structures, and auth headers. Instead of putting `if provider == "openai"` branches everywhere in the router logic, each provider gets its own Adapter class that implements 4 methods:

```python
translate_request(unified)       # your format → their format
call(native_payload, stream)     # make the HTTP call
translate_response(native)       # their response → your format
translate_stream_chunk(line)     # their SSE chunk → your SSE chunk
```

The router only speaks the unified format. It calls `adapter.translate_request()` → `adapter.call()` → `adapter.translate_response()`. Adding a new provider = adding one file. Zero changes to the router.

### 2. What is SSE (Server-Sent Events)?
A protocol where the server sends a stream of text events over a single HTTP connection that stays open. Each event is a line starting with `data: ` followed by JSON. The stream ends with `data: [DONE]`.

LLMs use this because they generate tokens sequentially. Rather than waiting for all tokens to be generated (could be 10+ seconds), the server sends each token the moment it's ready. The client renders them progressively — exactly like ChatGPT's typewriter effect.

### 3. What is a Circuit Breaker?
A pattern that tracks failure rates for a target and "trips" (opens) when failures exceed a threshold, preventing further calls to that target for a cooldown period.

Three states:
- **CLOSED** — normal, all requests go through, failures tracked in a sliding window
- **OPEN** — target is broken, skip it instantly, wait for cooldown
- **HALF-OPEN** — cooldown elapsed, allow one probe request to check if target recovered

Without it: if DigitalOcean goes down and you have 500 req/s, you'd have 500 threads/s all timing out after 60 seconds = 30,000 concurrent blocked threads = server OOM crash.

With it: after ~10 failures, the breaker opens. Subsequent requests skip DO in microseconds and go straight to the fallback.

### 4. What is Retryable vs Non-Retryable?
When an upstream provider returns an error, you need to decide: should I try the next provider, or should I return the error to the client immediately?

- **Retryable** (try next provider): 5xx errors, 429 rate-limit, timeouts, connection refused. These are *transient* — a different provider won't have this problem.
- **Non-retryable** (return error immediately): 400 bad request, 401/403 auth errors. These are *permanent for this request* — sending the same broken payload to a different provider won't help.

### 5. Why two httpx clients per adapter?
One for regular (non-streaming) calls with a 60-second read timeout, and one for streaming calls with a 90-second read timeout.

The reason: for non-streaming, DO processes the whole prompt and returns a complete response. For streaming, the first token might not arrive for several seconds (DO is still "thinking"). The streaming client needs a longer read-idle timeout or it disconnects before any tokens arrive.

---

## Likely Interview Questions and Strong Answers

### System Design Questions

**Q: Why did you use Flask instead of FastAPI or async frameworks?**

> Flask with synchronous httpx was chosen for simplicity and to match the spec requirements. The trade-off is clear: each streaming response holds a worker thread for the entire generation time. With 4 Gunicorn workers and 3-second responses, you saturate at ~4 concurrent streams. For production at high QPS, the right move is FastAPI + async httpx or aiohttp — async coroutines don't block a thread while waiting on I/O, so one worker handles thousands of concurrent streams. The current architecture is correct for low-to-medium load and easy to migrate to async.

**Q: How does the circuit breaker work exactly?**

> Each (provider, model) pair has its own state machine and a sliding window of the last 20 call outcomes. When I record a failure via `record_failure()`, it appends a 1 to the window. If the window has ≥10 samples and the failure rate is ≥50%, I set state to OPEN and record the timestamp. `is_open()` is called before every attempt — if OPEN, it checks if the cooldown has elapsed. If elapsed, it transitions to HALF-OPEN and returns False (allow one probe). If the probe succeeds, state goes CLOSED and the window clears. If it fails, I double the cooldown (capped at 5 minutes) and re-open.

**Q: What happens if the client disconnects mid-stream?**

> Flask raises a `GeneratorExit` exception inside the generator at the current yield point. My `dispatch_stream()` generator catches `GeneratorExit` and immediately returns. The `finally` block then calls `line_iter.close()` which propagates `GeneratorExit` into the httpx streaming generator, which exits the `with response.stream(...)` context manager, closing the TCP connection to the provider. This prevents wasted token generation and quota burn.

**Q: How do you decide whether to fall back during streaming?**

> There's a boolean flag `first_chunk_sent` that flips to True the moment the first chunk is yielded to the caller. If an error occurs and `first_chunk_sent` is False (error happened before any data went to client), I can silently try the next provider — the client hasn't received anything yet, so the switch is transparent. If `first_chunk_sent` is True, I can't fallback because the client has already received partial output. Switching providers would send incoherent content. Instead I yield an error chunk with `finish_reason: "error"` and close the stream cleanly.

**Q: How does the unified schema protect the client from provider changes?**

> The adapter layer is the only place that knows about provider-specific field names. The router, route handler, and all tests work exclusively with the unified schema. If DigitalOcean changes their API, only `app/adapters/do.py` changes. The client's code, all integration tests, and the router logic are untouched. This is the Adapter pattern — isolate vendor-specific knowledge into one pluggable class.

**Q: How do you handle the 401 case — why not try the fallback?**

> A 401 from the upstream provider means our API key for that provider is wrong or revoked. The fallback targets use the same API key (it's a router-level config). So sending the same request to a fallback target would also return 401 — you'd just waste time. The right response is to surface the 401 immediately to the operator (via logs and response headers) so they can fix the key. I classify 400, 401, 403 as non-retryable for this reason.

---

### Code / Implementation Questions

**Q: Walk me through what happens when `POST /v1/chat/completions` is called.**

> 1. Three middleware hooks run: assign a UUID as `X-Request-Id`, check Bearer token against the `ROUTER_API_KEYS` set, check body size is under 256 KB.
> 2. Route handler calls `_validate(body)` — a pure function that checks all fields and returns an error string or None.
> 3. Unknown fields are stripped via an allowlist.
> 4. If `stream: true` → `_handle_stream()`. Otherwise → `orchestrator.dispatch()`.
> 5. The orchestrator resolves the model from the registry, builds the fallback chain, and starts the attempt loop.
> 6. For each target: check circuit breaker, get adapter, call `translate_request()`, call `adapter.call()`, on success call `translate_response()` and return. On retryable error: record failure and continue. On non-retryable: raise `RouterError` immediately.
> 7. Route handler sets response headers and returns 200.

**Q: What does `translate_request` actually change for DigitalOcean?**

> Two things: it strips the `"do/"` prefix from the model ID (the client sends `"do/llama3.3-70b-instruct"`, DO expects `"llama3.3-70b-instruct"`), and it removes the `"metadata"` field which is a router-internal field that DO doesn't understand and would reject.

**Q: How do you test fallback without calling real providers?**

> I use `unittest.mock.patch.object(do_adapter, "call", side_effect=exc)` where `exc` is a fabricated `httpx.HTTPStatusError` with `status_code=503`. This patches the specific adapter instance's `call` method for the duration of the `with` block. The orchestrator's fallback loop catches the 503, classifies it as retryable, records a failure on the circuit breaker, and tries the mock adapter. I then assert the response has `provider: "mock"` and `X-Router-Attempts: 2`.

**Q: Why is there a `conftest.py` that blocks real HTTP?**

> Safety net. Any test that accidentally doesn't patch the adapter and somehow calls `httpx.Client.send()` for a real host would hit the live DO API — burning quota, being non-deterministic (result depends on network), and being slow. The conftest autouse fixture patches `httpx.Client.send` to raise `AssertionError` for any non-localhost call. This makes real-network calls impossible in CI and fails fast with a clear message.

---

### Trade-off and Design Questions

**Q: What would you change to make this handle 10,000 requests per second?**

> Three changes:
> 1. **Async runtime** — replace Flask + sync httpx with FastAPI + async httpx. Async I/O means one process handles thousands of concurrent in-flight requests instead of one per thread.
> 2. **Shared circuit breaker state** — currently each Gunicorn worker has its own in-process breaker. Worker A might have DO open while Worker B has it closed. Move state to Redis so all workers share the same view.
> 3. **Connection pool tuning** — currently `max_connections=100` per provider. At 10K RPS you'd need much larger pools, or a connection proxy layer.

**Q: What's the worst-case latency for a fallback request?**

> Sum of all attempt timeouts. With 3 providers each potentially timing out at 60 seconds, worst case is 180 seconds. In practice, the connect timeout (10s) fires first for dead providers. Real worst case is closer to 3 × 10s = 30 seconds. The `request_deadline_s=60` global deadline caps total time at 60 seconds — if the deadline is reached, no more targets are tried.

**Q: How would you add a new provider like Anthropic?**

> Create `app/adapters/anthropic.py` implementing `BaseAdapter`. The `translate_request` maps unified fields to Anthropic's format (e.g., split system message to top-level `system` field, map `max_tokens` which Anthropic requires). `translate_response` maps `content[0].text` to `choices[0].message.content`. Register it in `create_app()` with `adapters["anthropic"] = AnthropicAdapter(api_key)`. Add model entries to `build_registry()`. Zero changes to the router, orchestrator, or route handler.

**Q: Why not just retry the same provider instead of falling back?**

> Per-target retry is disabled by default for LLMs because retrying means re-sending the entire prompt, which re-charges tokens. If a provider is overloaded (503), sending the same request again immediately is likely to get another 503 and you've doubled your cost for nothing. Falling back to a different provider is more likely to succeed and doesn't re-bill the failing provider. A jittered retry against the same target can be optionally enabled per-provider in config for specific error types.

---

## One-Liner Answers to Quick Questions

| Question | Answer |
|---|---|
| What language / framework? | Python 3.11+, Flask 3.1, httpx 0.28 |
| How many tests? | 127 tests, 89% coverage |
| What CI tools? | GitHub Actions, ruff (lint), pytest (tests), pip-audit + bandit (security) |
| What's the auth model? | Router-issued Bearer tokens (not provider keys). Clients never see provider keys. |
| How does streaming work? | Flask generator + `stream_with_context`. Each chunk yielded as a `data: {...}\n\n` SSE event. |
| What's the fallback chain for llama3.3? | `do/llama3.3-70b-instruct` → `mock/echo` |
| What HTTP status when all providers fail? | 502 (general), 408 (all timeouts), 429 (all rate-limited) |
| Where is the DO API key stored? | In `httpx.Client` headers only. Never logged, never in responses. |
| What's the circuit breaker threshold? | 50% failures over last 20 calls (min 10 samples), 30s cooldown |
| Can the circuit breaker be shared across servers? | Not yet — in-process only. Redis-backed state is the next step. |
