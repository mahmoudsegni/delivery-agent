# Delivery Agent

**An AI delivery manager for on-prem teams.** It reads your Azure DevOps / TFS
backlog, drafts sprint plans, collects async standups over Telegram, watches
for blockers and scope creep, and assesses release readiness — then hands every
decision to a human for approval before touching anything.

[![CI](https://github.com/mahmoudsegni/delivery-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mahmoudsegni/delivery-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue)](backend/pyproject.toml)
[![React](https://img.shields.io/badge/react-18%2B-61dafb)](frontend/package.json)

## Why this exists

Off-the-shelf delivery tools stop at dashboards. LLM agents that write to your
tracker stop at "just let it". Delivery Agent sits in between: **the agent
proposes, a human approves, the tracker stays the source of truth.** It was
built for an on-prem Azure DevOps Server (TFS) environment where the backlog
can't leave the network and the bot has to reach devs where they actually are —
Telegram.

## What it does

- **Sprint drafting** — ingests the synced backlog, groups work into coherent
  sprint proposals with rationale, capacity-checked against the team's real
  availability (`features/proposals`, `features/capacity`)
- **Async standups over Telegram** — the bot asks, devs answer in chat,
  digests land in the team channel (`features/standup`)
- **Blocker & risk detection** — cross-references task state, history and
  standup answers to flag slipping work before it slips (`features/delivery`,
  `features/stream`)
- **Release readiness scoring** — a confidence model over live task state,
  not replayed history (see `backend/tests/test_burndown.py` for why that
  distinction matters) (`features/portfolio`, `backend/app/services/confidence_service.py`)
- **Code review proposals** — PR triage summaries drafted for the reviewer,
  never auto-merged (`features/prs`)
- **Conversational Q&A** — "what slipped this sprint?", "who's blocked?" over
  the live workspace (`features/ask`)

## Screenshots

**Command Center** — portfolio confidence, stream health, and the decision queue
where every agent escalation lands for a human call:

![Command Center](docs/screenshots/command-center.png)

**Kanban board** — synced work items with per-person capacity bars (red = over-allocated):

![Kanban board](docs/screenshots/kanban.png)

**Agent proposals** — the approval gate: agent reasoning, confidence, and the exact
change it wants to make. Nothing is executed until a human clicks Approve:

![Agent proposals](docs/screenshots/proposals.png)

## How it works

```
 ┌────────────┐     ┌─────────────┐     ┌──────────────┐     ┌────────────┐
 │ Azure      │────▶│  Sync       │────▶│ Agent core   │────▶│ Proposals  │
 │ DevOps/TFS │◀────│  workers    │     │ (LLM, tool   │     │ (drafts +  │
 │  on-prem   │ app.│  (Celery)   │     │  calling)    │     │  rationale)│
 └────────────┘     └─────────────┘     └──────┬───────┘     └─────┬──────┘
                        ▲                     │     ┌──────────────┤
                        │                     ▼     ▼              ▼
                   ┌────┴─────┐        ┌──────────────┐    ┌────────────┐
                   │PostgreSQL│        │ Approval gate│    │  Telegram  │
                   │  Redis   │        │  (human in   │    │    bot     │
                   └──────────┘        │   the loop)  │    └────────────┘
                                       └──────────────┘
```

Every mutating action is a **proposal** until a human approves it in the web
UI — sprint drafts, backlog edits, standup digests. The agent cannot write to
the tracker or message the team on its own. That invariant is the product.

## Quickstart

```bash
git clone https://github.com/mahmoudsegni/delivery-agent.git
cd delivery-agent
docker compose up -d                # 7 services, self-contained
docker compose exec backend python -m app.seed   # fictional demo team
# Web UI:      http://localhost:3000
# API docs:    http://localhost:8000/docs   (FastAPI OpenAPI)
```

No Azure DevOps instance required — the seed loads the fictional **Northwind
Dynamics** retail team with two sprints of realistic history. Real sync is
opt-in via `TFS_URL` / `TFS_PAT` (see [TFS-SYNC.md](TFS-SYNC.md)).

LLM features need any OpenAI-compatible endpoint (`LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`); the approval UI, board, and analytics work
without one.

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI · SQLAlchemy 2 (async) · Alembic · Celery (worker + beat) · PostgreSQL · Redis |
| Frontend | React 18 · TypeScript · Vite · Tailwind |
| Bot | Telegram (aiogram) — standups, digests, approvals |
| Sync | Azure DevOps Server / TFS REST API, offline-tolerant |
| Agent | LLM with tool calling, deterministic fallbacks when offline |

## Project layout

```
backend/
  app/
    services/        # domain logic: sprints, confidence, capacity, standup
    integrations/    # TFS provider + provider-agnostic task interface
    agent/           # LLM tool definitions, prompts, approval-gated actions
    api/             # FastAPI routers
  alembic/           # migrations, each with its "why" in the docstring
  tests/
frontend/src/
  features/          # one dir per feature: board, standup, proposals, …
scripts/             # TFS seed/sync tooling (tfs-pull, tfs-dump, tfs-load)
TFS-SYNC.md          # how on-prem sync works, and its limits
```

## Engineering notes

Things in here that were learned the hard way:

- **Live vs. replayed burndown** — confidence must be computed from live task
  state, never from the replayed series, or scores silently become functions
  of yesterday's score. Guarded by source-level regression tests.
- **Capacity defaults are a product decision** — the default hours-per-sprint
  migration ([016](backend/alembic/versions/016_capacity_default_60.py))
  documents why 40h made the demo team look broken.
- **Offline-first sync** — the TFS puller tolerates the on-prem server being
  unreachable for days and reconciles on reconnect; nothing in the UI blocks
  on the tracker.

## Integrations

The tracker is a plugin (`backend/app/integrations/task_provider.py` defines the
contract; `factory.py` picks the implementation from `INTEGRATION_PROVIDER`):

| Provider | Status | Notes |
|---|---|---|
| `mock` | ✅ built | Demo data, zero config |
| `dump` | ✅ built | Offline replay of a TFS capture — same mapping, no network |
| `tfs` | ✅ built | Azure DevOps Server / TFS, on-prem auth, area & iteration paths |
| `github` | ✅ built | GitHub Issues; board columns via `board:*` labels, sprints = milestones |
| `jira` | ✅ read + write | REST v3, status categories + review override, fixVersion = sprint; writes = fields PUT + transitions (PUSH_ENABLED), 410 → POST /search/jql |
| `linear` | ✅ read + write | GraphQL API, state types + review override, cycle = sprint, estimate = story points; writes = issueCreate/issueUpdate via resolved team + workflow state (PUSH_ENABLED) |

Adding one is implementing 5 async methods + 2 optional ones; `github_task_provider.py`
(~300 lines) is the reference. All providers honor `PUSH_ENABLED=false` as a hard
write-refusal at the provider level.

### MCP server

The backend exposes a read-only [Model Context Protocol](https://modelcontextprotocol.io)
endpoint at `POST /mcp` (Streamable HTTP) — ask Claude, Cursor or any MCP client
about the sprint instead of opening the dashboard:

```
claude mcp add --transport http delivery-agent http://localhost:8000/mcp
```

Four tools, all thin reads over the same services the REST API uses:
`delivery_snapshot` (stream health, confidence trend, active sprint, latest
standup, pending proposals — one call), `backlog`, `sprints`, and `forecast`
(Monte Carlo completion odds). No writes, no LLM key needed. On deployments
with `PASSCODE` set, MCP is gated off until token auth lands — run without a
passcode (local/self-host) to use it.

## Roadmap

- [x] GitHub Issues provider (dogfooded on this repo's own issues)
- [x] Jira + Linear providers (see Integrations above)
- [x] Read-only MCP server at `/mcp` (Claude/Cursor: snapshot, backlog, sprints, forecast)
- [ ] Slack adapter alongside Telegram
- [ ] Demo mode with pre-recorded agent sessions (no LLM key needed)
- [ ] Self-host quickscript + Helm chart

## Contributing & license

See [CONTRIBUTING.md](CONTRIBUTING.md) — keep sample data fictional, keep the
approval-gate invariant. MIT licensed.
