---
name: llm-resilient-api-patterns
description: "Production-grade retry patterns and error handling for LLM API calls. Covers exponential backoff with jitter, structured output validation, rate limit handling, circuit breaker patterns, and multi-provider fallback. Use when building AI agents that call OpenRouter, OpenAI, Anthropic, or any LLM API in production."
version: "1.0"
author: Hermes Agent (Django Research)
---

# LLM Resilient API Patterns

## Overview

LLM API calls fail. Rate limits, transient errors, context length exceeded, provider outages — production AI agents must handle all of gracefully. This skill covers battle-tested patterns for making LLM API calls reliable.

**Key principle:** An AI agent that crashes on a 429 rate limit is not production-ready. Build resilience into every API call.

## When to Use

- Building AI agents that make API calls to OpenRouter, OpenAI, Anthropic
- Setting up automated workflows that must complete despite transient failures
- Liberty Emporium: cron jobs, health checks, board monitors that use LLM APIs
- Any production code that depends on external AI services

## Error Types and Responses

| HTTP Code | Meaning | Strategy |
|-----------|---------|----------|
| 429 | Rate limit | Exponential backoff + retry |
| 500 | Server error | Retry with backoff |
| 502 | Bad gateway | Retry (transient) |
| 503 | Service unavailable | Retry + longer backoff |
| 400 | Bad request | Fix request, don't retry |
| 401 | Auth failure | Alert immediately, don't retry |
| 403 | Forbidden | Alert immediately, don't retry |
| 413 | Context too long | Reduce prompt, retry |
| 408 | Timeout | Retry with reduced prompt |

## Step 1: Basic Retry with Exponential Backoff

```python
import asyncio
import random
from functools import wraps

def retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=60.0):
    """Decorator for retrying async functions with exponential backoff + jitter."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    
                    # Don't retry auth errors
                    if hasattr(e, 'status_code') and e.status_code in (401, 403):
                        raise
                    
                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = delay * 0.5 * random.random()
                    total_delay = delay + jitter
                    
                    print(f"Attempt {attempt + 1} failed: {e}")
                    print(f"  Retrying in {total_delay:.1f}s...")
                    await asyncio.sleep(total_delay)
            
            raise last_exception
        return wrapper
    return decorator
```

## Step 2: Robust LLM Call Function

```python
import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass, field

@dataclass
class LLMResponse:
    content: str
    model: str
    tokens_used: int
    latency_ms: float
    provider: str
    cached: bool = False

@dataclass
class LLMConfig:
    api_key: str
    base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    model: str = "openrouter/owl-alpha"
    max_retries: int = 3
    timeout: int = 120
    max_tokens: int = 4096

class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
    
    async def call(
        self,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Make an LLM API call with full retry logic."""
        start = time.monotonic()
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.config.max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        
        last_error = None
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await self._make_request(payload)
                latency = (time.monotonic() - start) * 1000
                
                return LLMResponse(
                    content=result["choices"][0]["message"]["content"],
                    model=result["model"],
                    tokens_used=result["usage"]["total_tokens"],
                    latency_ms=round(latency, 1),
                    provider="openrouter",
                )
            
            except urllib.error.HTTPError as e:
                last_error = e
                error_body = e.read().decode() if e.fp else ""
                
                # Never retry auth errors
                if e.code in (401, 403):
                    raise LLMError(f"Auth failed: {error_body}") from e
                
                # Don't retry bad requests
                if e.code == 400:
                    raise LLMError(f"Bad request: {error_body}") from e
                
                # Retry rate limits and server errors
                if e.code == 429:
                    # Respect Retry-After header if present
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self._backoff_delay(attempt)
                    print(f"Rate limited. Waiting {delay:.1f}s...")
                    await asyncio.sleep(delay)
                elif e.code >= 500:
                    delay = self._backoff_delay(attempt)
                    print(f"Server error {e.code}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    raise LLMError(f"HTTP {e.code}: {error_body}") from e
            
            except urllib.error.URLError as e:
                last_error = e
                if attempt < self.config.max_retries:
                    delay = self._backoff_delay(attempt)
                    print(f"Connection error: {e.reason}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
            
            except asyncio.TimeoutError:
                last_error = TimeoutError("Request timed out")
                if attempt < self.config.max_retries:
                    delay = self._backoff_delay(attempt)
                    print(f"Timeout. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
        
        raise LLMError(f"Failed after {self.config.max_retries + 1} attempts") from last_error
    
    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        import random
        base = min(2 ** attempt, 60)  # Cap at 60s
        return base * random.random()  # Full jitter
    
    async def _make_request(self, payload: dict) -> dict:
        """Make the actual HTTP request."""
        loop = asyncio.get_event_loop()
        data = json.dumps(payload).encode()
        
        req = urllib.request.Request(
            self.config.base_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "HTTP-Referer": "https://agents.alexanderai.site",
                "X-Title": "Liberty Emporium Agent",
            },
            method="POST",
        )
        
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: json.loads(
                urllib.request.urlopen(req, timeout=self.config.timeout).read()
            )),
            timeout=self.config.timeout + 5,
        )

class LLMError(Exception):
    """Custom exception for LLM errors."""
    pass
```

## Step 3: Structured Output with Validation

```python
async def call_with_structured_output(
    client: LLMClient,
    messages: list[dict],
    schema: dict,
    max_validation_retries: int = 2,
) -> dict:
    """Call LLM with JSON schema and validate the output."""
    
    for attempt in range(max_validation_retries + 1):
        response = await client.call(
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        
        try:
            result = json.loads(response.content)
            # Validate with the schema
            import jsonschema
            jsonschema.validate(result, schema)
            return result
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            if attempt < max_validation_retries:
                # Add error feedback to messages for retry
                messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": f"Invalid JSON: {e}. Please fix and respond with valid JSON matching the schema."},
                ]
            else:
                raise LLMError(f"Invalid structured output after {max_validation_retries + 1} attempts: {e}") from e

# Usage example:
health_schema = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["healthy", "degraded", "down"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status"],
    "additionalProperties": False,
}

result = await call_with_structured_output(
    client,
    messages=[{"role": "user", "content": "Analyze the health report..."}],
    schema=health_schema,
)
```

## Step 4: Multi-Provider Fallback

```python
@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    priority: int  # Lower = tried first

class MultiProviderLLM:
    """Try multiple LLM providers in priority order."""
    
    def __init__(self, providers: list[ProviderConfig]):
        self.providers = sorted(providers, key=lambda p: p.priority)
        self.clients = {
            p.name: LLMClient(LLMConfig(
                api_key=p.api_key,
                base_url=p.base_url,
                model=p.model,
            ))
            for p in providers
        }
    
    async def call(
        self,
        messages: list[dict],
        **kwargs,
    ) -> LLMResponse:
        """Try each provider in order until one succeeds."""
        errors = []
        
        for provider in self.providers:
            try:
                response = await self.clients[provider.name].call(messages, **kwargs)
                response.provider = provider.name
                return response
            except Exception as e:
                errors.append((provider.name, str(e)))
                print(f"Provider {provider.name} failed: {e}")
                continue
        
        error_summary = "; ".join(f"{name}: {err}" for name, err in errors)
        raise LLMError(f"All providers failed: {error_summary}")

# Usage
multi_llm = MultiProviderLLM([
    ProviderConfig(
        name="openrouter",
        api_key="sk-or-...",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        model="openrouter/owl-alpha",
        priority=1,
    ),
    ProviderConfig(
        name="openai",
        api_key="sk-...",
        base_url="https://api.openai.com/v1/chat/completions",
        model="gpt-4o-mini",
        priority=2,
    ),
])

# Automatically falls back to OpenAI if OpenRouter fails
response = await multi_llm.call([{"role": "user", "content": "Hello"}])
```

## Step 5: Circuit Breaker Pattern

Prevent hammering a failing provider:

```python
import time
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Failing, reject fast
    HALF_OPEN = "half_open"  # Testing if recovered

class CircuitBreaker:
    """Circuit breaker for API calls."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
    
    def record_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
    
    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure_time > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                return True
            return False
        return True  # HALF_OPEN: allow test request

class ResilientLLMClient(LLMClient):
    """LLM client with circuit breaker."""
    
    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self.circuit = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=30.0,
        )
    
    async def call(self, messages: list[dict], **kwargs) -> LLMResponse:
        if not self.circuit.can_execute():
            raise LLMError(f"Circuit breaker OPEN for {self.config.model}. Try again later.")
        
        try:
            response = await super().call(messages, **kwargs)
            self.circuit.record_success()
            return response
        except Exception as e:
            self.circuit.record_failure()
            raise
```

## Step 6: Rate Limit Tracking

```python
from dataclasses import dataclass

@dataclass
class RateLimitInfo:
    remaining: int
    reset_time: float  # Unix timestamp
    limit: int

class RateLimitTracker:
    """Track rate limits across providers."""
    
    def __init__(self):
        self._limits: dict[str, RateLimitInfo] = {}
    
    def update(self, provider: str, headers: dict):
        """Update rate limit info from response headers."""
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        limit = headers.get("X-RateLimit-Limit")
        
        if remaining is not None:
            self._limits[provider] = RateLimitInfo(
                remaining=int(remaining),
                reset_time=float(reset) if reset else time.time() + 60,
                limit=int(limit) if limit else 0,
            )
    
    def should_throttle(self, provider: str) -> float:
        """Returns delay in seconds, or 0 if no throttling needed."""
        info = self._limits.get(provider)
        if info and info.remaining <= 1:
            delay = info.reset_time - time.time()
            return max(delay, 0)
        return 0
```

## Pitfalls & Workarounds

* **Retry amplification:** Retrying a failing call during an outage makes it worse.
  - **Fix:** Use circuit breakers to stop retrying during outages.
  - **Fix:** Implement backoff with jitter to spread retry timing.

* **Losing context on retry:** If you reduce context to handle 413 errors, you lose important information.
  - **Fix:** Summarize context instead of truncating.
  - **Fix:** Split large requests into multiple smaller ones.

* **Structured output still invalid after retries:** Sometimes the model consistently produces invalid JSON.
  - **Fix:** Use `strict: true` mode with JSON schema (OpenAI supports this).
  - **Fix:** Fall back to manual JSON extraction with `json.JSONDecoder().raw_decode()`.

* **Silent token limit exceeded:** Some providers truncate instead of erroring.
  - **Fix:** Check `finish_reason` in the response — `"length"` means truncated.
  - **Fix:** Pre-count tokens and split long prompts.

* **Provider-specific error formats:** Each provider has different error response formats.
  - **Fix:** Normalize errors into your own exception classes.
  - **Fix:** Use the `openai` or `anthropic` SDKs which handle this for you.

## Verification

```python
import asyncio

async def test_resilience():
    """Test the resilient LLM client."""
    
    client = LLMClient(LLMConfig(
        api_key="test-key",
        model="openrouter/owl-alpha",
        max_retries=2,
    ))
    
    # Test 1: Successful call
    response = await client.call([
        {"role": "user", "content": "Say hello in one word"}
    ])
    assert response.content
    assert response.tokens_used > 0
    print(f"✅ Response: {response.content} ({response.latency_ms}ms)")
    
    # Test 2: Structured output
    result = await call_with_structured_output(
        client,
        [{"role": "user", "content": "Return a greeting"}],
        {"type": "object", "properties": {"greeting": {"type": "string"}}, "required": ["greeting"]},
    )
    assert "greeting" in result
    print(f"✅ Structured: {result}")
    
    # Test 3: Circuit breaker trips
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
    cb.record_failure()
    cb.record_failure()
    assert not cb.can_execute()
    await asyncio.sleep(1.1)
    assert cb.can_execute()
    print("✅ Circuit breaker works")
    
    print("\nAll resilience tests passed!")

asyncio.run(test_resilience())
```

## Liberty Emporium Application

For cron jobs and automated agents:

```python
# In a cron job that uses LLM:
config = LLMConfig(
    api_key=os.environ["OPENROUTER_API_KEY"],
    model="openrouter/owl-alpha",
    max_retries=3,
    timeout=120,
)

client = ResilientLLMClient(config)

async def research_task():
    """Daily research task with full resilience."""
    try:
        response = await client.call([
            {"role": "system", "content": "You are a research assistant..."},
            {"role": "user", "content": "Find 3 new AI agent techniques..."},
        ])
        return response.content
    except LLMError as e:
        # Log the failure and try next time
        logger.error(f"Research task failed: {e}")
        return None
```
