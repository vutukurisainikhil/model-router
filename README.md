# Unified Model Router

A production-ready API gateway that accepts one standardised OpenAI-compatible inference schema, routes it to real upstream LLM providers, proxies live SSE streams back to the client with no buffering, and silently falls back to alternate providers when a primary target fails.

---

## What Is Built

### Architecture

```
Client
  │  POST /v1/chat/completions  (Bearer <ROUTER_KEY>)
  ▼
┌─────────────────────────────────────────────────────┐
│  Flask API Gateway                                  │
│                                                     │
│  Middleware                                         │
│  ├── assign request-id                              │
│  ├── authenticate (router API key)                  │
│  └── enforce body-size limit (256 KB default)       │
│                                                     │
│  Route handler (/v1/chat/completions)               │
│  ├── validate unified schema                        │
│  └── Orchestrator                                   │
│      ├── ModelRegistry  → resolve unified model     │
│      │                    build fallback chain      │
│      ├── CircuitBreaker → skip open targets         │
│      └── Attempt loop                               │
│          ├── Adapter.translate_request()            │
│          ├── Adapter.call()  ──────────────────────►│──► Upstream provider
│          ├── Adapter.translate_response()           │◄── response / SSE chunks
│          └── return unified response / stream       │
└─────────────────────────────────────────────────────┘
```

### Components

| Layer | File | Responsibility |
|---|---|---|
| App factory | `app/__init__.py` | Wire adapters, registry, circuit breaker, orchestrator |
| Config | `app/config.py` | Env-driven settings, loaded once at startup |
| Middleware | `app/middleware.py` | Request-ID, Bearer auth, body-size guard |
| Route | `app/routes/chat.py` | Validate → dispatch → set response headers |
| Orchestrator | `app/router/orchestrator.py` | Fallback attempt loop, error classification |
| Circuit Breaker | `app/router/circuit_breaker.py` | Sliding-window per `(provider, model)` |
| Model Registry | `app/router/model_registry.py` | Unified model ID → primary + fallback chain |
| Base Adapter | `app/adapters/base.py` | ABC: `translate_request`, `call`, `translate_response`, `translate_stream_chunk` |
| DO Adapter | `app/adapters/do.py` | DigitalOcean Serverless Inference (OpenAI-compat) |
| Mock Adapter | `app/adapters/mock.py` | In-process deterministic adapter (tests / dev) |
| SSE helpers | `app/streaming/sse.py` | `sse_event()`, `parse_sse_line()`, `SSE_DONE` |

### Features Shipped

**F1 — Unified API & Schema Translation**
- `POST /v1/chat/completions` with OpenAI-compatible request/response schema
- Per-provider payload translation (strips `metadata`, rewrites model IDs)
- Strict input validation with typed error codes

**F2 — Live SSE Streaming**
- True pipe: chunks yielded one-at-a-time via `stream_with_context`
- Zero client-side buffering
- Dual `httpx.Client` per adapter (separate read timeouts for stream vs non-stream)

**F3 — Resilient Fallback Routing**
- Ordered fallback chain per model (`primary → fallback[]`)
- Retryable vs non-retryable HTTP status classification
- Pre-first-byte streaming fallback: transparent to client
- Mid-stream abort rule: no fallback after first chunk; yields `finish_reason: error`
- Sliding-window circuit breaker per `(provider, model)` with exponential back-off

---

## How It Works

### Request Lifecycle (non-streaming)

1. Middleware assigns `X-Request-Id`, validates Bearer token, checks body size.
2. Route handler validates the unified schema.
3. Orchestrator resolves the model via `ModelRegistry`, builds a chain: `[primary] + fallbacks`.
4. For each target in the chain:
   - Skip if `CircuitBreaker.is_open()` → mark attempt as `skipped_breaker`.
   - Call `Adapter.translate_request()` → native payload.
   - `Adapter.call(stream=False)` → raw provider response.
   - On success: `translate_response()` → unified response; record success; return.
   - On retryable error (5xx, 429, timeout): record failure; try next target.
   - On non-retryable error (400, 401, 403): record failure; surface immediately (no fallback).
5. If all targets fail: raise `RouterError` → 502 (or 429/408 based on last error class).
6. Response headers: `X-Request-Id`, `X-Router-Provider`, `X-Router-Model`, `X-Router-Latency-Ms`, `X-Router-Attempts`.

### Streaming Lifecycle

Same steps 1–3. Then:
- Pre-checks model existence; returns `400` before committing to `text/event-stream`.
- Each chunk from `Adapter.call(stream=True)` → `translate_stream_chunk()` → `data: {...}\n\n`.
- Retryable error before first chunk → silent fallback to next target.
- Any error after first chunk → `data: {"finish_reason":"error"}\n\n` + `data: [DONE]\n\n`.

### Circuit Breaker State Machine

```
CLOSED ──(failure rate ≥ threshold, min N samples)──► OPEN
  ▲                                                      │
  │                                                   cooldown
  │                                                      ▼
  └──(success)──── HALF_OPEN ◄──(cooldown elapsed)──── OPEN
                       │
                   (failure)
                       │
                       └──► OPEN (cooldown × 2, capped 5 min)
```

Default: window=20, min_samples=10, failure_rate=50%, cooldown=30 s.

### Error Classification

| Upstream HTTP | Retryable | Client HTTP | `error.code` |
|---|---|---|---|
| 400 | No | 400 | `upstream_error` |
| 401 / 403 | No | 401 / 403 | `upstream_error` |
| 408 / 504 | Yes | 408 | `upstream_timeout` |
| 429 | Yes | 429 | `rate_limited` |
| 5xx | Yes | 502 | `upstream_error` |
| Network timeout | Yes | 408 | `upstream_timeout` |
| Connect error | Yes | 502 | `upstream_error` |

---

## Quick Start

```bash
git clone https://github.com/vutukurisainikhil/model-router.git
cd model-router
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your ROUTER_API_KEYS and DO_INFERENCE_API_KEY
python -m flask --app wsgi:app run --port 8000
```

---

## Manual Testing (curl)

> The default API key in `.env.example` is `dev-router-key-1`. Replace with whatever you set in `ROUTER_API_KEYS`.

**1. Health check**
```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

**2. Non-streaming — mock provider (no network, instant)**
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock/echo","messages":[{"role":"user","content":"hello"}]}' \
  | python3 -m json.tool
```

**3. Non-streaming — real DigitalOcean provider**
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"do/llama3.3-70b-instruct","messages":[{"role":"user","content":"say hi"}]}' \
  | python3 -m json.tool
```

**4. See all router headers**
```bash
curl -si http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"do/llama3.3-70b-instruct","messages":[{"role":"user","content":"hi"}]}' \
  | grep "X-Router"
```
Expected:
```
X-Router-Provider: do
X-Router-Model: llama3.3-70b-instruct
X-Router-Latency-Ms: 2100
X-Router-Attempts: 1
X-Router-Fallback-Chain: do/llama3.3-70b-instruct,mock/echo
```

**5. Streaming — tokens arrive word by word**
```bash
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"do/llama3.3-70b-instruct","stream":true,"messages":[{"role":"user","content":"count from 1 to 5"}]}'
```

**6. Streaming — readable word-by-word output**
```bash
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"do/llama3.3-70b-instruct","stream":true,"messages":[{"role":"user","content":"count from 1 to 5"}]}' \
  | grep --line-buffered -o '"content": "[^"]*"' \
  | tr -d '"content: '
```

**7. Error cases**
```bash
# Wrong API key → 401
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wrong-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock/echo","messages":[{"role":"user","content":"hi"}]}' \
  | python3 -m json.tool

# Unknown model → 400 model_not_found
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-5","messages":[{"role":"user","content":"hi"}]}' \
  | python3 -m json.tool

# Missing messages → 400 invalid_request
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-router-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock/echo"}' \
  | python3 -m json.tool
```

**Production (gunicorn):**
```bash
gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
```

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `ROUTER_API_KEYS` | *(required)* | Comma-separated bearer tokens the router accepts |
| `DO_INFERENCE_API_KEY` | *(required)* | Secret key for DigitalOcean Serverless Inference |
| `DO_INFERENCE_BASE_URL` | `https://inference.do-ai.run/v1` | Override for local proxy or staging |
| `DO_DEFAULT_MODEL` | `llama3.3-70b-instruct` | Native model name for the `do/` default |
| `MAX_BODY_BYTES` | `262144` (256 KB) | Hard request body limit |
| `APP_ENV` | `development` | `production` disables debug |
| `SERVICE_VERSION` | `0.1.0` | Echoed in `/health` response |

---

## Supported Models

| Unified ID | Provider | Native model | Fallback |
|---|---|---|---|
| `do/llama3.3-70b-instruct` | DigitalOcean | `llama3.3-70b-instruct` | `mock/echo` |
| `do/openai-gpt-4o-mini` | DigitalOcean | `openai-gpt-4o-mini` | `mock/echo` |
| `mock/echo` | In-process | — | — |

Add a new provider by implementing `BaseAdapter` and registering it in `create_app()`.

---

## Testing

```bash
pytest                                          # 127 tests
pytest --cov=app --cov-report=term-missing      # with coverage (target ≥ 85%)
pytest tests/unit/                              # unit only — no network
pytest tests/integration/                       # integration — network blocked by conftest guard
```

Test breakdown: 1 health · 10 DO adapter · 6 mock adapter · 7 model registry · 19 validation · 24 streaming · 14 circuit breaker · 18 chat integration · 16 stream integration · 12 fallback integration.

---

## CI / CD

GitHub Actions workflow at [.github/workflows/ci.yml](.github/workflows/ci.yml).
Runs on every push and pull request to `main`:

1. **Lint** — `ruff check` + `ruff format --check`
2. **Test** — `pytest --cov=app --cov-fail-under=85`
3. **Security** — `pip-audit` (dependency CVEs) + `bandit -ll` (code scan)

Matrix: Python 3.11 and 3.12.

---

## Structured Logging

Every successful dispatch emits a JSON log event to stdout:

```json
{
  "ts": "2026-06-27T05:55:53Z",
  "level": "INFO",
  "logger": "model_router",
  "msg": "dispatch_complete",
  "request_id": "b7473d02-...",
  "unified_model": "do/llama3.3-70b-instruct",
  "served_by": "do",
  "native_model": "llama3.3-70b-instruct",
  "total_latency_ms": 812,
  "attempts": [
    {"provider": "do", "model": "llama3.3-70b-instruct", "outcome": "success", "latency_ms": 812}
  ]
}
```

Fallback example — when primary fails and backup serves:
```json
{
  "attempts": [
    {"provider": "do", "model": "llama3.3-70b-instruct", "outcome": "error", "error_class": "http_503", "latency_ms": 120},
    {"provider": "mock", "model": "echo", "outcome": "success", "latency_ms": 2}
  ],
  "served_by": "mock"
}
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 unauthorized` | Wrong or missing bearer token | Check `ROUTER_API_KEYS` in `.env`; token must match exactly |
| `400 model_not_found` | Unknown model ID | Use `do/llama3.3-70b-instruct`, `do/openai-gpt-4o-mini`, or `mock/echo` |
| `502 upstream_error` | DO API key invalid or service down | Check `DO_INFERENCE_API_KEY`; verify DO status page |
| Stream not streaming (buffered behind proxy) | Nginx/proxy buffering | Add `proxy_buffering off` or ensure `X-Accel-Buffering: no` is respected |
| `413` on large prompts | Body exceeds 256 KB | Increase `MAX_BODY_BYTES` in `.env` |

---

## Security & Privacy

- `DO_INFERENCE_API_KEY` is held only in the `httpx.Client` headers object — never logged, never echoed in responses, never appears in tracebacks.
- `ROUTER_API_KEYS` are stored as a plain in-memory set. Rotate by restarting the process (or use a secrets manager with env injection for zero-downtime).
- No prompt or response body content is ever logged. Log events contain only metadata (model, provider, token counts, latency).
- Input validation rejects oversized bodies before any processing occurs.

---

## License & Contributing

MIT License. Contributions welcome — open an issue or PR.
For new provider adapters, implement `BaseAdapter` in `app/adapters/` and register in `create_app()`. No changes to the router or route layer required.

---

## Response Headers

| Header | Example | Meaning |
|---|---|---|
| `X-Request-Id` | `a1b2c3d4` | Stable correlation ID for the full request |
| `X-Router-Provider` | `do` | Adapter that ultimately served the response |
| `X-Router-Model` | `llama3.3-70b-instruct` | Native model name used |
| `X-Router-Latency-Ms` | `812` | Wall-clock time for the upstream call |
| `X-Router-Attempts` | `2` | Number of targets tried (1 = no fallback needed) |

---

## Considerations & Trade-offs

### High QPS / Throughput

**Synchronous Flask + httpx is the primary bottleneck.** Each request blocks a worker thread for the full upstream latency (often 1–5 s for LLMs).

| Scenario | Behaviour |
|---|---|
| Streaming under Gunicorn | Each streamed response holds a worker for the entire generation time. With 4 workers you saturate at ~4 concurrent streams. |
| Non-streaming bursts | Token throughput limited to `workers × (1 / avg_latency)`. 4 workers × 2 req/s = ~8 RPS ceiling. |
| Circuit breaker under load | Open breakers cut latency to near-zero (no upstream call). Fallback adds one extra attempt's latency. |
| `MAX_BODY_BYTES` | Body is read fully before dispatch; protects workers from slow-loris attacks but caps prompt size. |

**Mitigation paths (not yet built):**
- Replace Flask/httpx with an async stack (FastAPI + httpx async or aiohttp). Streaming becomes truly concurrent with far fewer threads.
- Add a connection pool size cap per provider (`httpx.Client(limits=...)`).
- Implement request queuing with backpressure instead of direct 5xx under load.

### Circuit Breaker

**Pros:** Stops wasting latency budget on a known-broken provider; protects the upstream from thundering-herd retries during incidents.

**Cons:** State is in-process per worker. With multiple Gunicorn workers (or pods), each worker maintains its own breaker — a provider can be OPEN in worker A and CLOSED in worker B. Fix: move state to Redis or a shared sidecar.

### Fallback Chain

**Pros:** Transparent to clients; audit trail via `X-Router-Attempts` and structured logs.

**Cons:** Worst-case latency = sum of all attempt timeouts before the first success. With 3 providers each timing out at 60 s, a client waits up to 3 minutes. Set aggressive `request_deadline_s` and per-target connect timeouts.

### Streaming Fallback Limitation

Pre-first-byte fallback is transparent. But once the first SSE chunk is sent the HTTP 200 + headers are already committed — a mid-stream failure can only yield an error sentinel chunk; the client must handle `finish_reason: error` gracefully.

### Secret Handling

`DO_INFERENCE_API_KEY` is read from env at startup and never logged or echoed. `ROUTER_API_KEYS` are stored as a plain set in memory — rotate by restarting the process (or use a secrets manager with dynamic reload for zero-downtime rotation).

### Missing for Production

- Async runtime (FastAPI / Starlette) for real concurrency
- Shared circuit breaker state (Redis)
- Per-provider rate-limit budgets and token-per-minute tracking
- Structured JSON logging with PII redaction
- `/metrics` endpoint (Prometheus)
- Retry jitter to avoid thundering herd after outage recovery
- Distributed tracing (OpenTelemetry)
- CI/CD pipeline (GitHub Actions)
