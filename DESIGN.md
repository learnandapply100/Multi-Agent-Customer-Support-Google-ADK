# DESIGN

> Copy this file to `DESIGN.md` and answer it **before you write code**. Revise it as you
> learn, but keep the first version in git history. A stranger grades it (`G-DESIGN`), so
> write for someone who has never seen your code. The five headings are fixed; keep them.

## Components

| Part | Runs as | Port |
|---|---|---|
| PostgreSQL | own process | 5432 |
| MCP Toolbox | own process | 5001 |
| Security Judge | own process (A2A) | 10002 |
| Data Masker | own process (A2A) | 10003 |
| Phoenix | own process | 6006 |
| Web server | own process | 8000 |
| CLI | own process | none |
| Pipeline, Sanitizer, Guardrail, support agent | inside the web or CLI process | none |
| Gemini, Mem0 | outside cloud services | none |

Judge and Masker are separate so they can be applied across multiple products. They check for things that generally need to be checked for any product. While Guardrail is product specific so it is in the same application.

## Responsibilities

| Rule | Enforced in | Where and why |
|---|---|---|
| R-1 ownership | code, plus prompt | SQL filter and bound email. The prompt only explains the reply to give. |
| R-2 every-turn work is a step | code | Recall and save are pipeline steps. The agent has no memory tool. |
| R-3 no mutating tool | code | The toolset has three tools, and the database user has no UPDATE or DELETE right. |
| R-4 fail loud | code | A guard that raises ends the turn with an `error` event. |
| R-5 save only the user's words | code | The save step is only ever given the message. |
| R-6 guardrail scope | prompt | It is a judgement about meaning, so it can only be a prompt. Measured on both sides by the eval. |
| R-7 over-long memories | code | `MAX_CHARS = 500` in `support/memory.py`. |
| R-8 guard changes only its own thing | code | The Masker only replaces pattern matches. The client checks the count matches the text. |
| R-9 explicit verdict | code | Fixed `allow` or `block` shape. Anything else is an error. |
| R-10 traces outlive the process | code | Phoenix is its own process with storage on disk. |
| R-11 enums by schema | code, plus prompt | `CHECK` constraint and allowed values in the tool. The prompt lists them to help the model. |

Note: The “look the order up before logging” rule (P08) is prompt only. There was no away of stopping the model from attempting a tool call through code. The SQL still rejects it.

## Communication

| Hop | From and to | Protocol |
|---|---|---|
| 1 | Browser to web server | HTTP `POST /api/chat` (the CLI calls the pipeline directly) |
| 2 | Web server to pipeline | function call |
| 3 | Pipeline to Sanitizer | function call |
| 4 | Pipeline to Security Judge | A2A JSON-RPC over HTTP, method `message/send` |
| 5 | Pipeline to Guardrail | in-process, then HTTPS to Gemini |
| 6 | Pipeline to Mem0 (recall) | HTTPS |
| 7 | Agent to Gemini, and agent to Toolbox to Postgres | HTTPS, then MCP, then SQL |
| 8 | Pipeline to Data Masker | A2A JSON-RPC over HTTP, method `message/send` |
| 9 | Pipeline to Mem0 (save) | HTTPS |
| 10 | Web server back to browser | NDJSON, one event per line |

A verdict on the wire is a JSON-RPC result whose data part looks like:
`{"verdict": "block", "reason": "matched pattern: sql: drop table", "source": "pattern", "tokens": {"in": 0, "out": 0}}`

## State

| What | Where | How long | Customer data? | Who can read it |
|---|---|---|---|---|
| Conversation session | memory of the web or CLI process | until logout or restart | yes | nobody outside the process |
| Memories | Mem0, keyed by email | until deleted | yes | anyone with the Mem0 key |
| Traces | Phoenix, on disk | kept across restarts | yes, full messages and replies | the developer |
| Run logs | `runs/*.json` | until deleted | yes, email and message | the developer |
| Action log, orders, users | Postgres | until the next reset | yes | database users, by their grants |

When an order changes after a memory was saved, the memory is not updated. The agent’s instruction says to trust a tool call over a memory for order facts. That rule is prompt only.

## Trade-offs

| Step | Middle time | Share of a turn |
|---|---|---|
| Sanitizer | 0 ms | 0% |
| Security Judge | 1,793 ms | 18% |
| Guardrail | 1,838 ms | 19% |
| Memory recall | 782 ms | 8% |
| Support agent | 4,602 ms | 47% |
| Data Masker | 32 ms | 0% |
| Memory save | 762 ms | 8% |
| **Total** | **9,809 ms** | |

Latency target missed. T-LAT-P50 is 9,433 ms against 8,000 ms. This is a cost of using two guards. They take about 50% of the turn time. However the guards are needed in order to prevent data leakage and other malicious attacks. Further investigation can be done to reduce the latency while still keeping the necessary checks in place.
Yes the Guardrail fail is closed. When Gemini is slow, the customer gets an error and no answer. 
`T-MEM-MINSCORE` was changed to 0.16. The evidence is in BUILD_LOG.md