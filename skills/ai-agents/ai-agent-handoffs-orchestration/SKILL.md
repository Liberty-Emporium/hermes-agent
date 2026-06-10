---
name: ai-agent-handoffs-orchestration
description: "Design and implement multi-agent handoff patterns where specialized agents delegate tasks to each other. Covers OpenAI Agents SDK handoff architecture, input filtering, context transfer, and orchestration patterns for building reliable multi-agent systems. Use when building multi-agent workflows, customer support routing, or any system where agents need to delegate to specialists."
version: "1.0"
author: Hermes Agent (Django Research)
---

# AI Agent Handoffs & Orchestration

## Overview

Handoffs allow an agent to delegate tasks to another specialized agent. This is the foundation of multi-agent orchestration — instead of one generalist agent trying to do everything, you build specialist agents that each excel at their domain and hand off work between them.

**Key insight:** Handoffs are represented as **tools** to the LLM. If there's a handoff to an agent named `RefundAgent`, the tool is called `transfer_to_refund_agent`. The LLM decides when to invoke it based on the handoff description.

## When to Use

- Customer support routing (order status → refunds → FAQs → escalation)
- Multi-step workflows requiring different expertise (research → writing → review)
- Any system where task complexity exceeds a single agent's context window
- Liberty Emporium: routing customer issues to specialized fix agents

## Architecture

```
User Input
    ↓
Triage Agent (determines intent)
    ↓ handoff
┌───────────────────────────────────────────┐
│  Order Agent  │  Refund Agent  │  FAQ Agent │
│  (specialist) │  (specialist)  │ (specialist)│
└───────────────────────────────────────────┘
    ↓ (if can't resolve)
Escalation Agent (human handoff)
```

## Step 1: Define Specialist Agents

Each agent should have a narrow, well-defined role:

```python
from agents import Agent

order_agent = Agent(
    name="Order Agent",
    instructions="You handle order status inquiries. Look up the order, "
                 "provide tracking info, and estimated delivery. "
                 "Be concise and helpful.",
    handoffs=[],  # Leaf agent - no further delegation
    tools=[lookup_order, get_tracking],
)

refund_agent = Agent(
    name="Refund Agent",
    instructions="You handle refund requests. Check eligibility, "
                 "process refunds, and explain the timeline. "
                 "Always confirm the order number first.",
    handoffs=[],
    tools=[check_refund_eligibility, process_refund],
)
```

## Step 2: Create the Triage Agent with Handoffs

```python
from agents import Agent, handoff

triage_agent = Agent(
    name="Triage Agent",
    instructions="You route customer inquiries to the right specialist. "
                 "Determine the customer's intent and hand off to the "
                 "appropriate agent. You never try to answer directly.",
    handoffs=[
        handoff(order_agent),
        handoff(refund_agent),
    ],
)
```

## Step 3: Customize Handoffs with Input Filtering

Control what context gets passed during handoffs:

```python
from agents import Agent, handoff, HandoffInputData

def filter_order_context(input_data: HandoffInputData) -> HandoffInputData:
    """Only pass order-related messages to the order agent."""
    # Filter conversation history to relevant messages
    filtered_history = [
        msg for msg in input_data.input_history
        if "order" in str(msg).lower() or "tracking" in str(msg).lower()
    ]
    return HandoffInputData(
        input_history=filtered_history,
        pre_handoff_messages=input_data.pre_handoff_messages,
        run_context=input_data.run_context,
    )

order_handoff = handoff(
    order_agent,
    input_filter=filter_order_context,
    on_handoff=lambda ctx, order_number: print(
        f"Handing off to Order Agent for order #{order_number}"
    ),
)
```

## Step 4: Add Input Validation with `input_type`

Require structured input for handoffs:

```python
from pydantic import BaseModel

class RefundInput(BaseModel):
    order_number: str
    reason: str
    amount: float | None = None

refund_handoff = handoff(
    refund_agent,
    input_type=RefundInput,
    tool_description_override="Transfer to refund agent with order details",
)
```

## Step 5: Implement Human-in-the-Loop Handoffs

For escalation to human agents:

```python
from agents import Agent, handoff

escalation_agent = Agent(
    name="Escalation Agent",
    instructions="You handle cases that automated agents cannot resolve. "
                 "Collect all context and prepare a summary for the human agent.",
    tools=[create_support_ticket, notify_human_agent],
)

# Triage agent can escalate
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route inquiries. If no specialist can help after 2 attempts, escalate.",
    handoffs=[
        handoff(order_agent),
        handoff(refund_agent),
        handoff(escalation_agent),  # Last resort
    ],
)
```

## Step 6: Add Guardrails to Handoffs

Combine handoffs with guardrails for safety:

```python
from agents import Agent, input_guardrail, RunContextWrapper, GuardrailFunctionOutput

@input_guardrail
async def no_homework_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    input: str | list,
) -> GuardrailFunctionOutput:
    """Prevent misuse - reject off-topic requests."""
    homework_keywords = ["math homework", "solve this equation", "do my homework"]
    is_homework = any(kw in str(input).lower() for kw in homework_keywords)
    return GuardrailFunctionOutput(
        output_info={"is_homework": is_homework},
        tripwire_triggered=is_homework,
    )

safe_triage = Agent(
    name="Safe Triage Agent",
    instructions="Route customer inquiries safely.",
    handoffs=[handoff(order_agent), handoff(refund_agent)],
    input_guardrails=[no_homework_guardrail],
)
```

## Execution Modes

### Blocking (Default)
Guardrail runs first. If it trips, the expensive model never runs. **Use for:** cost-sensitive applications.

### Parallel
Guardrail runs alongside the agent. Both complete before checking. **Use for:** latency-sensitive applications where you want the response fast but still want to flag issues.

## Recommended Prompts for Handoff Agents

Each specialist agent's instructions should include:

1. **Domain scope** — what this agent handles
2. **Boundaries** — what this agent does NOT handle
3. **Handoff trigger** — when to pass to another agent
4. **Output format** — consistent response structure

Example:
```
You are the Order Agent. You handle:
- Order status lookups
- Tracking information
- Delivery estimates

You do NOT handle:
- Refunds (hand off to Refund Agent)
- Account issues (hand off to Account Agent)

If the request is outside your scope, use the appropriate handoff tool.
Always confirm the order number before looking anything up.
```

## Pitfalls & Workarounds

* **Infinite handoff loops:** Agent A hands off to B, B hands back to A.
  - **Fix:** Track handoff history in context. Reject handoffs to agents already in the chain.
  - **Fix:** Set `max_handoffs=5` limit in run config.

* **Context loss during handoffs:** Specialist agent doesn't have conversation history.
  - **Fix:** Use `input_filter` to pass relevant history.
  - **Fix:** Store shared state in `RunContextWrapper`.

* **LLM ignores handoff tools:** Model tries to answer directly instead of delegating.
  - **Fix:** Make handoff tool descriptions very specific and compelling.
  - **Fix:** Add "You MUST use handoff tools for any request outside your expertise" to instructions.

* **Over-specialization:** Too many tiny agents, LLM can't decide which to use.
  - **Fix:** Keep agents focused but not microscopic. 3-5 specialists is usually optimal.
  - **Fix:** Make the triage agent's instructions very clear about routing criteria.

## Verification

Test your handoff system:

```python
import asyncio
from agents import Runner

async def test_handoffs():
    runner = Runner()
    
    # Test 1: Order inquiry routes to Order Agent
    result = await runner.run(
        triage_agent,
        "Where is my order #12345?"
    )
    assert "order" in result.final_output.lower() or "tracking" in result.final_output.lower()
    
    # Test 2: Refund request routes to Refund Agent
    result = await runner.run(
        triage_agent,
        "I want a refund for order #12345"
    )
    assert "refund" in result.final_output.lower()
    
    # Test 3: Off-topic request triggers guardrail
    result = await runner.run(
        safe_triage,
        "Help me with my math homework"
    )
    assert result.guardrail_tripped

asyncio.run(test_handoffs())
```

## Liberty Emporium Application

For customer support automation:

```
Customer Message
    ↓
Triage Agent (classifies: billing / technical / sales / other)
    ↓
┌─────────────────────────────────────────────┐
│ Billing    │ Technical  │ Sales    │ Human  │
│ Agent      │ Agent      │ Agent    │ Escal  │
│ (Stripe,   │ (Tailscale,│ (pricing,│ (Jay)  │
│  invoices) │  install)  │  demo)   │        │
└─────────────────────────────────────────────┘
```

Each agent has access to relevant tools and knowledge, and can escalate to Jay when needed.
