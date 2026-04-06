# AGENTS.md

## Repository-wide instructions

This repository contains a Kakao bot system with:
- MessengerBotR API2 gateway bot
- single FastAPI app
- SQLite
- APScheduler
- socket push delivery

## Working rules
- For large changes, start in Ask mode and produce an implementation plan before writing code.
- Preserve the single FastAPI architecture.
- Do not refactor unrelated features unless required by imports/tests.
- Keep normal-room output separate from admin-room operational/error output.
- Keep room-name exact match behavior.
- Keep event idempotency based on logId.

## HanAll briefing rules
- The authoritative HanAll business spec is in:
  - `docs/hanall_monitoring_prompt.md`
- Treat that file as the single source of truth for HanAll research behavior and report structure.
- Do not shorten or paraphrase that spec unless explicitly asked.
- Keep 08:00 KST collect-once behavior.
- Room publish jobs must reuse the stored daily artifact.
- Publish jobs must not silently rerun research if artifact is missing.
- Public room output and admin room output must remain separated.
- Public output must be concise and Kakao-friendly.
- Admin output should preserve the rich report structure.

## Kimi / Formula rules
- Keep OpenAI Python SDK style with base_url=https://api.moonshot.ai/v1
- Keep Formula tool loading and fibers call flow correct.
- Handle all tool_calls.
- Preserve reasoning-related context when available.
- Keep max_iterations, tool_timeout, overall_deadline.

## Files likely relevant for HanAll changes
- `app/services/hanall_research_service.py`
- `app/scheduler.py`
- `app/config.py`
- `app/schemas.py`
- `app/repositories.py`
- `app/models.py`
- `config/prompts.yaml`
- tests related to HanAll briefing

## Validation
- Run relevant tests after changes.
- Report changed files, tests run, and remaining assumptions.