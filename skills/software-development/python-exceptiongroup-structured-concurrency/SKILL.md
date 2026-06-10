---
name: python-exceptiongroup-structured-concurrency
description: "Modern Python error handling with ExceptionGroup (except*), TaskGroup, and structured concurrency patterns. Covers Python 3.11+ features for handling multiple concurrent failures, graceful shutdown, and building resilient async applications. Use when writing async Python code, building agent tools, or handling multiple potential failure points."
version: "1.0"
author: Hermes Agent (Django Research)
---

# Python ExceptionGroup & Structured Concurrency

## Overview

Python 3.11 introduced `ExceptionGroup` and `except*` for handling multiple unrelated exceptions simultaneously. Combined with `asyncio.TaskGroup`, this enables **structured concurrency** — a paradigm where concurrent tasks have well-defined lifetimes and all errors are properly propagated.

**Key insight:** Traditional `asyncio.gather()` returns the first exception and hides the rest. `TaskGroup` collects ALL failures and raises them together as an `ExceptionGroup`.

## When to Use

- Running multiple concurrent API calls where you want to know about ALL failures
- Building resilient agent tools that call multiple services
- Health checks that test multiple endpoints simultaneously
- Any async code where silent failure of background tasks is unacceptable
- Liberty Emporium: health check system that reports all failing services, not just the first

## Core Concepts

### ExceptionGroup

An `ExceptionGroup` is a container for multiple unrelated exceptions:

```python
# Creating an ExceptionGroup
eg = ExceptionGroup("multiple failures", [
    ConnectionError("API timeout"),
    ValueError("Invalid response"),
    TimeoutError("Rate limit exceeded"),
])

# Catching the whole group
try:
    raise eg
except ExceptionGroup as eg:
    print(f"Caught {len(eg.exceptions)} failures")
    for exc in eg.exceptions:
        print(f"  - {type(exc).__name__}: {exc}")
```

### except* — Selective Exception Handling

`except*` catches only exceptions of a specific type from an ExceptionGroup:

```python
try:
    raise ExceptionGroup("mixed errors", [
        ValueError("bad input"),
        TypeError("wrong type"),
        ValueError("another bad input"),
        KeyError("missing_key"),
    ])
except* ValueError as val_errs:
    # Catches BOTH ValueError instances
    print(f"Value errors: {len(val_errs.exceptions)}")
    for e in val_errs.exceptions:
        print(f"  {e}")
except* TypeError:
    print("Type error caught")
# KeyError propagates up because it's not caught
```

### asyncio.TaskGroup — Structured Concurrency

```python
import asyncio

async def fetch(url: str) -> dict:
    """Simulate an API call."""
    await asyncio.sleep(0.1)
    if "fail" in url:
        raise ConnectionError(f"Failed: {url}")
    return {"url": url, "status": 200}

async def main():
    results = {}
    async with asyncio.TaskGroup() as tg:
        task1 = tg.create_task(fetch("https://api1.example.com"))
        task2 = tg.create_task(fetch("https://api2.example.com"))
        task3 = tg.create_task(fetch("https://fail.example.com"))
    # All tasks completed successfully if we reach here
    results["api1"] = task1.result()
    results["api2"] = task2.result()
    results["api3"] = task3.result()

try:
    asyncio.run(main())
except* ConnectionError as errs:
    for e in errs.exceptions:
        print(f"Failed: {e}")
```

## Step 1: Replace asyncio.gather with TaskGroup

### Before (gather — loses errors):

```python
async def check_all_services(urls: list[str]) -> list:
    """Old way — only reports first failure."""
    tasks = [check_service(url) for url in urls]
    # gather returns first exception, other failures are silent
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Now you have to manually filter exceptions from results
    return results
```

### After (TaskGroup — reports all failures):

```python
async def check_all_services(urls: list[str]) -> dict:
    """New way — reports ALL failures."""
    results = {}
    tasks = {}
    
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tasks[url] = tg.create_task(check_service(url))
    
    # All succeeded
    return {url: task.result() for url, task in tasks.items()}

# Caller handles failures:
try:
    results = await check_all_services(urls)
except* ConnectionError as errs:
    for e in errs.exceptions:
        print(f"Connection failed: {e}")
except* TimeoutError as errs:
    for e in errs.exceptions:
        print(f"Timed out: {e}")
```

## Step 2: Build a Resilient Health Checker

```python
import asyncio
import time
import urllib.request
from dataclasses import dataclass, field

@dataclass
class HealthResult:
    url: str
    status: str  # "UP", "DOWN", "TIMEOUT", "ERROR"
    response_time_ms: float = 0.0
    error: str | None = None

@dataclass
class HealthReport:
    timestamp: str
    results: list[HealthResult] = field(default_factory=list)
    up_count: int = 0
    down_count: int = 0

async def check_endpoint(url: str, timeout: float = 10.0) -> HealthResult:
    """Check a single endpoint."""
    start = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        req = urllib.request.Request(url, headers={"User-Agent": "HealthCheck/1.0"})
        response = await asyncio.wait_for(
            loop.run_in_executor(None, urllib.request.urlopen, req),
            timeout=timeout,
        )
        elapsed = (time.monotonic() - start) * 1000
        return HealthResult(
            url=url,
            status="UP",
            response_time_ms=round(elapsed, 1),
        )
    except asyncio.TimeoutError:
        return HealthResult(url=url, status="TIMEOUT", error=f"Timeout after {timeout}s")
    except OSError as e:
        return HealthResult(url=url, status="DOWN", error=str(e))
    except Exception as e:
        return HealthResult(url=url, status="ERROR", error=str(e))

async def run_health_checks(urls: list[str]) -> HealthReport:
    """Check all endpoints concurrently, report ALL results."""
    from datetime import datetime, timezone
    
    report = HealthReport(timestamp=datetime.now(timezone.utc).isoformat())
    
    tasks = {}
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tasks[url] = tg.create_task(check_endpoint(url))
    
    # All checks completed (none raised unhandled exceptions)
    for url, task in tasks.items():
        result = task.result()
        report.results.append(result)
        if result.status == "UP":
            report.up_count += 1
        else:
            report.down_count += 1
    
    return report

# Usage
async def main():
    urls = [
        "https://agents.alexanderai.site",
        "https://liberty-community-hub.alexanderai.site",
        "https://gymforge.alexanderai.site",
    ]
    
    report = await run_health_checks(urls)
    print(f"Health Check — {report.timestamp}")
    print(f"UP: {report.up_count} | DOWN: {report.down_count}")
    for r in report.results:
        status_icon = "✅" if r.status == "UP" else "❌"
        print(f"  {status_icon} {r.url}: {r.status} ({r.response_time_ms}ms)")
        if r.error:
            print(f"     Error: {r.error}")

asyncio.run(main())
```

## Step 3: Implement Graceful Shutdown

```python
import asyncio
import signal

class AgentService:
    def __init__(self):
        self._shutdown = asyncio.Event()
        self._tasks = set()
    
    async def start(self):
        """Start the service with graceful shutdown handling."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown.set)
        
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._health_server())
                tg.create_task(self._worker_loop())
                tg.create_task(self._shutdown_wait())
        except* Exception as errs:
            for e in errs.exceptions:
                print(f"Service error: {e}")
    
    async def _shutdown_wait(self):
        """Wait for shutdown signal, then cancel other tasks."""
        await self._shutdown.wait()
        raise asyncio.CancelledError("Shutdown requested")
    
    async def _health_server(self):
        """HTTP health check endpoint."""
        while True:
            await asyncio.sleep(1)
            # Serve health checks...
    
    async def _worker_loop(self):
        """Main worker loop."""
        while True:
            await asyncio.sleep(5)
            # Process work items...

# Usage
service = AgentService()
asyncio.run(service.start())
```

## Step 4: Handle Partial Failures

When you want results from successful tasks even when some fail:

```python
async def check_with_partial_results(urls: list[str]) -> tuple[dict, list]:
    """Return successful results AND a list of failures."""
    results = {}
    failures = []
    
    tasks = {}
    async with asyncio.TaskGroup() as tg:
        for url in urls:
            tasks[url] = tg.create_task(check_endpoint(url))
    
    for url, task in tasks.items():
        result = task.result()
        if result.status == "UP":
            results[url] = result
        else:
            failures.append(result)
    
    return results, failures

# Usage with error handling
try:
    results, failures = await check_with_partial_results(urls)
except* Exception as errs:
    # Handle unexpected errors
    for e in errs.exceptions:
        print(f"Unexpected: {e}")
    results, failures = {}, []

print(f"Successful: {len(results)}, Failed: {len(failures)}")
```

## Step 5: Nested ExceptionGroups

ExceptionGroups can be nested — handle them recursively:

```python
def flatten_exceptions(group: BaseExceptionGroup) -> list[Exception]:
    """Flatten nested ExceptionGroups into a flat list."""
    flat = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            flat.extend(flatten_exceptions(exc))
        else:
            flat.append(exc)
    return flat

# Usage
try:
    # Some operation that raises nested ExceptionGroups
    pass
except* Exception as errs:
    all_errors = flatten_exceptions(errs)
    for e in all_errors:
        print(f"  {type(e).__name__}: {e}")
```

## Pitfalls & Workarounds

* **TaskGroup cancels all tasks on first unhandled exception:** If one task raises and it's not caught by `except*`, ALL other tasks are cancelled.
  - **Fix:** Always catch exceptions from TaskGroup with `except*`.
  - **Fix:** Use `return_exceptions=True` pattern if you need gather-like behavior.

* **except* cannot catch ExceptionGroup itself:** `except* ValueError` catches ValueError instances inside ExceptionGroups, but not the group itself.
  - **Fix:** Use regular `except ExceptionGroup` to catch the group.

* **TaskGroup requires Python 3.11+:** Not available in older Python versions.
  - **Workaround:** Use `asyncio.gather(*tasks, return_exceptions=True)` for older versions.
  - **Workaround:** Use the `trio` library which has structured concurrency for older Python.

* **Signal handlers in asyncio:** `loop.add_signal_handler()` doesn't work on Windows.
  - **Fix:** Use `signal.signal()` for cross-platform code (but it has limitations in asyncio).
  - **Fix:** Use `try/except KeyboardInterrupt` as a fallback.

* **ExceptionGroup.subgroup() is useful but tricky:** It returns `None` if no matching exceptions exist.
  - **Fix:** Always check for `None`: `if (sub := eg.subgroup(ValueError)) is not None:`

## Verification

```python
import asyncio

async def test_exceptiongroup():
    """Verify ExceptionGroup behavior."""
    
    # Test 1: TaskGroup raises ExceptionGroup on failure
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(asyncio.sleep(0))
            raise ValueError("test error")
    except* ValueError as errs:
        assert len(errs.exceptions) == 1
        assert str(errs.exceptions[0]) == "test error"
    
    # Test 2: Multiple failures collected
    try:
        async with asyncio.TaskGroup() as tg:
            async def fail(msg):
                raise ConnectionError(msg)
            tg.create_task(fail("api1 down"))
            tg.create_task(fail("api2 down"))
    except* ConnectionError as errs:
        assert len(errs.exceptions) == 2
    
    # Test 3: except* selective catching
    try:
        raise ExceptionGroup("mixed", [
            ValueError("v1"), TypeError("t1"), ValueError("v2")
        ])
    except* ValueError as v:
        assert len(v.exceptions) == 2
    except* TypeError as t:
        assert len(t.exceptions) == 1
    
    print("All tests passed!")

asyncio.run(test_exceptiongroup())
```

## Liberty Emporium Application

Use TaskGroup for the health check system:

```python
# In the health check cron job:
async def check_all_apps():
    apps = {
        "Dashboard": "https://agents.alexanderai.site",
        "LCH": "https://liberty-community-hub.alexanderai.site",
        "GymForge": "https://gymforge.alexanderai.site",
        "FloodClaims": "https://floodclaims.alexanderai.site",
    }
    
    results = {}
    async with asyncio.TaskGroup() as tg:
        for name, url in apps.items():
            results[name] = tg.create_task(check_endpoint(url))
    
    report_lines = []
    for name, task in results.items():
        r = task.result()
        icon = "✅" if r.status == "UP" else "❌"
        report_lines.append(f"{icon} {name}: {r.status} ({r.response_time_ms}ms)")
    
    return "\n".join(report_lines)
```
