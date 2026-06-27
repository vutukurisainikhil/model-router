# Sequence Diagrams

Detailed call-execution flows with file names and function names for every major path through the model router.

---

## 1. Non-Streaming: `POST /v1/chat/completions` (stream: false)

```mermaid
sequenceDiagram
    participant C as Client
    participant MW as middleware.py<br/>register_middleware()
    participant R as chat.py<br/>chat_completions()
    participant O as orchestrator.py<br/>Orchestrator
    participant MR as model_registry.py<br/>ModelRegistry
    participant CB as circuit_breaker.py<br/>CircuitBreaker
    participant DA as do.py<br/>DOAdapter
    participant DO as DigitalOcean<br/>API

    C->>MW: POST /v1/chat/completions<br/>Bearer dev-router-key-1

    MW->>MW: _assign_request_id()<br/>g.request_id = uuid4()
    MW->>MW: _check_body_size()<br/>content_length ≤ 256KB?
    MW->>MW: _authenticate()<br/>token in ROUTER_API_KEYS?

    MW->>R: request passes middleware

    R->>R: _validate(body)<br/>check model, messages,<br/>temperature, max_tokens
    R->>R: strip unknown fields<br/>via _ALLOWED_FIELDS

    R->>O: dispatch(payload, request_id)

    O->>MR: resolve("do/llama3.3-70b-instruct")
    MR-->>O: {primary: {provider:"do", model:"llama3.3"},<br/>fallbacks: [mistral-24b, llama3.1-8b, mock/echo]}

    O->>O: _build_chain(entry)<br/>→ [do/llama3.3, do/mistral-24b, do/llama3.1-8b, mock/echo]

    loop For each target in chain
        O->>CB: is_open("do", "llama3.3")
        CB-->>O: False (CLOSED)

        O->>O: _get_adapter("do")
        O->>DA: translate_request(unified_payload)<br/>strip "do/" prefix, remove metadata
        DA-->>O: native_payload

        O->>DA: call(native_payload, stream=False)
        DA->>DO: POST /v1/chat/completions<br/>Authorization: Bearer DO_KEY
        DO-->>DA: HTTP 200 + full JSON response
        DA-->>O: httpx.Response (raw)

        O->>CB: record_success("do", "llama3.3")
        O->>DA: translate_response(raw)<br/>add provider:"do", prefix model with "do/"
        DA-->>O: unified_response dict

        O-->>R: (unified, "do", "llama3.3", latency_ms, attempts)
    end

    R->>R: jsonify(unified)
    R->>R: set headers:<br/>X-Request-Id, X-Router-Provider,<br/>X-Router-Model, X-Router-Latency-Ms,<br/>X-Router-Attempts, X-Router-Fallback-Chain

    R-->>C: HTTP 200 + JSON body + headers
```

---

## 2. Streaming: `POST /v1/chat/completions` (stream: true)

```mermaid
sequenceDiagram
    participant C as Client
    participant MW as middleware.py
    participant R as chat.py<br/>_handle_stream()
    participant O as orchestrator.py<br/>dispatch_stream()
    participant CB as circuit_breaker.py
    participant DA as do.py<br/>DOAdapter._stream_lines()
    participant SSE as sse.py<br/>sse_event()
    participant DO as DigitalOcean

    C->>MW: POST /v1/chat/completions<br/>stream: true

    MW->>MW: _assign_request_id()
    MW->>MW: _check_body_size()
    MW->>MW: _authenticate()

    MW->>R: _handle_stream(payload, request_id, orchestrator)

    R->>O: get_routing_info("do/llama3.3")<br/>pre-check model exists → 400 if not
    O-->>R: ("do", "llama3.3-70b-instruct")

    R->>O: get_chain_str("do/llama3.3")
    O-->>R: "do/llama3.3-70b-instruct,do/mistral-24b-instruct,do/llama3.1-8b-instruct,mock/echo"

    R-->>C: HTTP 200 headers sent immediately<br/>Content-Type: text/event-stream<br/>X-Accel-Buffering: no<br/>X-Router-Fallback-Chain: ...

    Note over R,C: HTTP 200 + headers committed.<br/>Body streams from here.

    R->>R: event_generator() wrapped in<br/>stream_with_context()

    loop event_generator iterates dispatch_stream()

        O->>CB: is_open("do", "llama3.3")
        CB-->>O: False

        O->>DA: translate_request(unified_payload)
        DA-->>O: native_payload

        O->>DA: call(native_payload, stream=True)<br/>returns _stream_lines() generator

        DA->>DO: POST /v1/chat/completions stream=True<br/>Authorization: Bearer DO_KEY

        DO-->>DA: HTTP 200 headers (connection open)
        DA->>DA: resp.raise_for_status()<br/>before first yield → pre-stream error check

        loop For each SSE line from DO
            DO-->>DA: "data: {delta: {role: assistant}}"
            DA-->>O: raw SSE line (yield)
            O->>DA: translate_stream_chunk(line)<br/>add provider:"do", prefix model
            DA-->>O: unified chunk dict
            O-->>R: yield chunk (first_chunk_sent = True)
            R->>SSE: sse_event(chunk)<br/>→ "data: {...}\n\n"
            R-->>C: flush chunk to socket immediately
        end

        DO-->>DA: "data: [DONE]"
        DA-->>O: "[DONE]" line
        O->>CB: record_success("do", "llama3.3")
        O->>O: return (generator stops)

    end

    R->>SSE: SSE_DONE = "data: [DONE]\n\n"
    R-->>C: "data: [DONE]\n\n"
    Note over R,C: Flask sends zero-length HTTP chunk.<br/>TCP connection closes.
```

---

## 3. Fallback Path (DO returns 503 → walks 4-level chain → Mock)

```mermaid
sequenceDiagram
    participant O as orchestrator.py<br/>dispatch()
    participant CB as circuit_breaker.py
    participant DA as do.py<br/>DOAdapter
    participant MA as mock.py<br/>MockAdapter
    participant DO as DigitalOcean

    Note over O: Chain for do/llama3.3-70b-instruct:<br/>llama3.3-70b → mistral-24b → llama3.1-8b → mock/echo

    O->>CB: is_open("do", "llama3.3-70b") → False
    O->>DA: translate_request() + call()
    DA->>DO: POST /v1/chat/completions (llama3.3-70b)
    DO-->>DA: HTTP 503
    DA-->>O: raises HTTPStatusError(503)
    O->>O: _is_retryable(503) → True
    O->>CB: record_failure("do", "llama3.3-70b")

    O->>CB: is_open("do", "mistral-24b") → False
    O->>DA: translate_request() + call()
    DA->>DO: POST /v1/chat/completions (mistral-24b)
    DO-->>DA: HTTP 503
    DA-->>O: raises HTTPStatusError(503)
    O->>CB: record_failure("do", "mistral-24b")

    O->>CB: is_open("do", "llama3.1-8b") → False
    O->>DA: translate_request() + call()
    DA->>DO: POST /v1/chat/completions (llama3.1-8b)
    DO-->>DA: HTTP 503
    DA-->>O: raises HTTPStatusError(503)
    O->>CB: record_failure("do", "llama3.1-8b")

    Note over O: All DO targets exhausted. Final backstop: mock/echo.

    O->>CB: is_open("mock", "echo") → False
    O->>MA: translate_request() → pass-through
    O->>MA: call(native_payload, stream=False)
    MA-->>O: response dict (in-process, no network)
    O->>CB: record_success("mock", "echo")
    O->>MA: translate_response() → unified dict
    O->>O: log dispatch_complete<br/>attempts: 4 total
    O-->>R: (unified, "mock", "echo", latency_ms, attempts)

    Note over R: X-Router-Provider: mock<br/>X-Router-Attempts: 4<br/>X-Router-Fallback-Chain: do/llama3.3-70b,...,mock/echo
```

---

## 4. Circuit Breaker Opens (repeated failures trip the breaker)

```mermaid
sequenceDiagram
    participant O as orchestrator.py<br/>dispatch()
    participant CB as circuit_breaker.py<br/>CircuitBreaker
    participant DA as do.py<br/>DOAdapter
    participant MA as mock.py<br/>MockAdapter

    Note over CB: After 10+ samples with ≥50% failures<br/>circuit trips OPEN for "do/llama3.3-70b"

    O->>CB: is_open("do", "llama3.3-70b")
    CB-->>O: True (OPEN — skip without calling DO)
    O->>O: attempts.append({outcome:"skipped_breaker"})

    O->>CB: is_open("do", "mistral-24b") → False
    O->>DA: translate_request() + call() [mistral-24b]
    DA-->>O: HTTP 503
    O->>CB: record_failure("do", "mistral-24b")

    O->>CB: is_open("do", "llama3.1-8b") → False
    O->>DA: translate_request() + call() [llama3.1-8b]
    DA-->>O: HTTP 503
    O->>CB: record_failure("do", "llama3.1-8b")

    O->>CB: is_open("mock", "echo") → False
    O->>MA: translate_request() + call()
    MA-->>O: response dict (in-process)
    O->>CB: record_success("mock", "echo")
    O-->>R: (unified, "mock", "echo", latency_ms, attempts)

    Note over CB: After cooldown_s (30s), state → HALF_OPEN.<br/>Next call probes DO.<br/>Success → CLOSED. Failure → OPEN (doubled cooldown).
```

---

## 5. Cost-Aware Routing (`prefer_cheapest: true`)

```mermaid
sequenceDiagram
    participant C as Client
    participant R as chat.py<br/>chat_completions()
    participant O as orchestrator.py<br/>dispatch()
    participant CO as costs.py<br/>input_cost()
    participant MA as mock.py<br/>MockAdapter
    participant DA as do.py<br/>DOAdapter

    C->>R: POST /v1/chat/completions<br/>{model: "do/openai-gpt-4o-mini",<br/> metadata: {prefer_cheapest: true}}

    R->>O: dispatch(payload)

    O->>O: prefer_cheapest = payload["metadata"]["prefer_cheapest"] → True
    O->>O: _build_chain(entry, prefer_cheapest=True)

    Note over O,CO: Normal chain order:<br/>gpt-4o-mini($0.15) → llama3.3-70b($0.60) → llama3.1-8b($0.10) → mock($0.00)

    O->>CO: input_cost("mock/echo") → 0.00
    O->>CO: input_cost("do/llama3.1-8b-instruct") → 0.10
    O->>CO: input_cost("do/openai-gpt-4o-mini") → 0.15
    O->>CO: input_cost("do/llama3.3-70b-instruct") → 0.60

    Note over O: Sorted chain (cheapest first):<br/>mock($0.00) → llama3.1-8b($0.10) → gpt-4o-mini($0.15) → llama3.3-70b($0.60)

    O->>MA: call mock/echo first (cheapest, $0.00)
    MA-->>O: response dict (in-process, instant)
    O-->>R: (unified, "mock", "echo", latency_ms, attempts=[1])
    R-->>C: HTTP 200, X-Router-Provider: mock, X-Router-Attempts: 1
```

---

## 6. Auth Scope Rejection (key lacks `chat` scope)

```mermaid
sequenceDiagram
    participant C as Client
    participant MW as middleware.py<br/>_authenticate()
    participant R as chat.py<br/>require_scope("chat")

    C->>MW: POST /v1/chat/completions<br/>Authorization: Bearer readonly-key

    MW->>MW: token in ROUTER_API_KEYS? → True
    MW->>MW: g.auth_scopes = ROUTER_KEY_SCOPES["readonly-key"]<br/>→ frozenset({"health"})
    MW-->>R: request passes auth (key is valid)

    R->>R: require_scope("chat") decorator runs
    R->>R: "chat" in g.auth_scopes? → False
    R-->>C: HTTP 403<br/>{"error": {"code": "forbidden",<br/>"message": "Your API key does not have the 'chat' scope"}}

    Note over C,R: LLM never called. Zero upstream cost.
```
