# Documentation

Start with [the README](../README.md) for the shortest first-run path, or
[Setup and operations](agent/SETUP.md) for installation, MCP connection,
background collection, upgrades, and troubleshooting. For day-to-day use,
read [the agent guide](agent/AGENT_GUIDE.md) and
[tool reference](agent/TOOL_REFERENCE.md).

Two audiences are separated below. Setup serves both users and agents helping
them configure the server; the other agent documents explain tool use.

## `agent/` — for whatever calls this server

Served to connecting clients as MCP resources, so **what a client reads is
exactly what is in these files**. Written for a model deciding which tool to
call and how to interpret the result.

| File | Resource URI |
|---|---|
| [TOOL_REFERENCE.md](agent/TOOL_REFERENCE.md) | `poe2market://tools` |
| [AGENT_GUIDE.md](agent/AGENT_GUIDE.md) | `poe2market://guide` |
| [DATA_MODEL.md](agent/DATA_MODEL.md) | `poe2market://data-model` |
| [RATE_LIMITS.md](agent/RATE_LIMITS.md) | `poe2market://rate-limits` |
| [SETUP.md](agent/SETUP.md) | `poe2market://setup` |

Editing one of these changes what every connecting client sees. Keep them
factual and current — a stale claim here becomes a confident wrong answer.

## `maintainers/` — for whoever keeps the server running

Not exposed over MCP. Written for a human changing the code.

| File | Covers |
|---|---|
| [DESIGN.md](maintainers/DESIGN.md) | Architecture, decisions, invariants, how to extend, testing |
| [FINDINGS.md](maintainers/FINDINGS.md) | What was measured against the live API, with numbers |

`FINDINGS.md` is the evidence behind the rules in `agent/`. When behaviour
changes because the API changed, record the measurement there.
