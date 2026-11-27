"""Application configuration using Pydantic BaseSettings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://delivery:delivery@localhost:5433/delivery_agent"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LLM Configuration
    llm_provider: str = "litellm"
    llm_model: str = "gpt-4"
    # Daily token budget for the Command Center usage strip (issue #60).
    # Tokens; 0 = off (the default). When set, the strip turns amber at 80%.
    llm_daily_token_budget: int = 0

    # Demo mode: replay pre-recorded agent responses (proposals, standup
    # digests, briefs, ask answers) instead of calling any LLM, so the full
    # UI + live agent loop runs with zero API keys.
    demo_mode: bool = False

    # Deep Agent Configuration
    # LangChain init_chat_model format is "provider:model". Reuse the working
    # Groq setup so proposals run on the same model as the /analyze endpoint.
    deep_agent_model: str = "groq:openai/gpt-oss-120b"

    # API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    azure_api_key: str = ""
    azure_api_base: str = ""
    groq_api_key: str = ""

    # Groq Configuration (for synchronous AI analysis)
    groq_model: str = "openai/gpt-oss-120b"
    # gpt-oss is a reasoning model: reasoning tokens are billed against
    # max_tokens BEFORE any content is emitted. Keep reasoning effort low for
    # structured JSON extraction and leave generous headroom for the answer,
    # otherwise reasoning can consume the whole budget and content comes back
    # empty (finish_reason="length").
    groq_max_tokens: int = 4000
    groq_reasoning_effort: str = "low"

    # Security
    secret_key: str = "change-me-in-production"

    # Optional passcode gate for exposed deployments (issue #8).
    # Empty = open (LAN default). When set, requests need the da_passcode
    # cookie (unlocked at /api/v1/passcode) or an X-Passcode header.
    passcode: str = ""

    # Shared secret letting calendar apps fetch the ICS feed past the
    # passcode gate (they can't do the cookie unlock). Empty = no exemption.
    ics_token: str = ""

    # Debug mode
    debug: bool = True

    # ------------------------------------------------------------------
    # Integration layer (task sync)
    # ------------------------------------------------------------------
    # Which TaskProvider backs work-item sync. "mock" for the demo, "tfs" for
    # a real Azure DevOps / TFS instance.
    integration_provider: str = "mock"
    # When true, tasks created/updated in our system are pushed to the provider.
    push_enabled: bool = True

    # Azure DevOps / TFS
    # Cloud:   https://dev.azure.com/<org>
    # On-prem: https://<host>/<collection>   e.g. .../DefaultCollection
    tfs_url: str = ""
    tfs_pat: str = ""             # Personal Access Token
    tfs_project: str = ""
    tfs_work_item_type: str = "Task"
    # REST api-version. Cloud is 7.1; on-prem Azure DevOps Server pins to the
    # release (2022 -> 7.1, 2020 -> 6.0, 2019 -> 5.0).
    tfs_api_version: str = "7.1"
    # Optional area-path scope, e.g. "Northwind\\Retail Platform".
    # Empty pulls the whole project — fine for a demo instance, not for a real one.
    tfs_area_path: str = ""
    # Hard cap on work items per sync (0 = unbounded). Ids come back
    # newest-changed first, so the cap keeps the most recently active items.
    tfs_max_items: int = 0
    # Comma-separated work item types to mirror. Empty = the provider's
    # DEFAULT_SYNC_TYPES (real delivery work only — excludes code-review and
    # feedback items, shared steps/parameters, which inflate the raw count).
    tfs_sync_types: str = ""
    # Where DumpTaskProvider reads a scripts/tfs-dump.py capture from
    # (INTEGRATION_PROVIDER=dump). Mounted read-only in docker-compose.
    tfs_dump_dir: str = "/dump"

    # GitHub Issues (INTEGRATION_PROVIDER=github)
    # repo as "owner/name"; token needs repo scope for write-back (PUSH_ENABLED)
    github_repo: str = ""
    github_token: str = ""

    # Jira Cloud (INTEGRATION_PROVIDER=jira)
    # Site base URL; auth is email + API token (Basic), JQL scopes the sync.
    jira_url: str = ""             # e.g. https://yourteam.atlassian.net
    jira_email: str = ""
    jira_api_token: str = ""
    jira_jql: str = "ORDER BY updated DESC"
    # Comma-separated status names that mean "review" whatever their
    # statusCategory says.
    jira_review_statuses: str = "In Review,Review,Code Review"

    # Linear (INTEGRATION_PROVIDER=linear)
    # API key auth (Settings -> Security & access); team key scopes writes
    # and sprints, inferred from the first synced issue when empty.
    linear_api_key: str = ""
    linear_team_key: str = ""          # e.g. "ENG"
    # Comma-separated workflow state names that mean "review" whatever
    # their type says.
    linear_review_statuses: str = "In Review,Review,Code Review"

    tfs_sync_interval: int = 300  # periodic pull interval (seconds)
    # Whether the periodic beat job auto-pulls from the external task provider.
    # OFF by default: the mock provider mints a fresh external_id each run, so a
    # scheduled sync inserts new tasks every interval and floods the DB. Flip
    # TASK_SYNC_ENABLED=true to re-enable the scheduled pull. The manual
    # "Sync from TFS" endpoint is unaffected by this flag.
    task_sync_enabled: bool = False
    # Sync-stall watchdog: alert when the newest pull is older than this many
    # hours or ended in "error". Re-check cadence (seconds) for the beat job.
    sync_stale_hours: int = 6
    sync_check_interval: int = 900

    # ------------------------------------------------------------------
    # Telegram standup bot
    # ------------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""        # used to build t.me deep links
    # Daily scheduled standup (celery-beat crontab, UTC)
    standup_schedule_cron: str = "0 9 * * *"
    # Hard cutoff after which a round is closed + analyzed regardless of
    # outstanding responders (seconds).
    standup_cutoff_seconds: int = 1800
    # Mid-round nudge: DM members who haven't answered yet (15 min after the
    # default 09:00 start, 15 min before the cutoff).
    standup_nudge_cron: str = "15 9 * * *"

    # ------------------------------------------------------------------
    # Webex bot (REST API — bot tokens do not expire)
    # ------------------------------------------------------------------
    webex_bot_token: str = ""
    webex_bot_email: str = ""       # <name>@webex.bot — drops self-messages (inbound)
    webex_api_base: str = "https://webexapis.com/v1"
    # Comma-separated room ids and/or person emails that receive the brief.
    webex_brief_targets: str = ""
    webex_webhook_secret: str = ""  # X-Spark-Signature HMAC key (inbound)
    # Where people on the network open the app (e.g. http://192.168.1.20:3002).
    # Used to put clickable links in Webex messages (standup form, blockers).
    app_public_url: str = ""
    # Test mode: when set (an email), EVERY Webex message goes to this address
    # instead of the real recipient, labelled with who it was meant for.
    webex_redirect_to: str = ""
    # Fail-closed: without a redirect, nothing is sent unless this is
    # explicitly true. Going live is a deliberate act, never a missing setting.
    webex_live: bool = False

    # ------------------------------------------------------------------
    # Release-confidence trend
    # ------------------------------------------------------------------
    # Daily job that snapshots per-sprint confidence (celery-beat crontab, UTC).
    # End-of-day capture so the value reflects the full day's progress.
    confidence_snapshot_cron: str = "30 23 * * *"

    # ------------------------------------------------------------------
    # Coding-agent PR loop
    # ------------------------------------------------------------------
    # Which CodingAgent backs "assign to AI agent". "mock" runs the whole loop
    # locally (no creds); "azure_devops" opens real Azure Repos PRs + polls
    # Azure Pipelines CI. Mirrors the integration_provider mock/tfs split.
    coding_agent_provider: str = "mock"
    # Guardrails — the agent's authority is bounded.
    coding_agent_max_iterations: int = 3       # revise-loop hard cap
    coding_agent_cost_ceiling: float = 5.0     # per-task USD ceiling
    coding_agent_allowed_dirs: str = ""        # comma-sep allowlist; "" = any
    coding_agent_max_diff_lines: int = 800     # reject oversized diffs
    require_tests: bool = True                  # spec/diff must include tests
    coding_agent_ci_poll_interval: int = 30    # CI poll cadence (seconds)
    coding_agent_max_ci_polls: int = 40        # bound so "CI never returns" ends
    # Mock-only: make the first CI check fail (exercises the CI gate).
    mock_ci_fail_first: bool = False
    # Azure Repos / Pipelines (single configured repo). Reuses tfs_url + tfs_pat.
    azure_repo_project: str = ""
    azure_repo_id: str = ""                     # repo name or GUID
    azure_repo_default_branch: str = "main"
    azure_pipeline_id: str = ""                 # build definition id for CI gate
    # Comma-separated repos to pre-populate the dispatch form's repository
    # dropdown. The dropdown also includes any repo a prior run targeted, and
    # the user can type a new one ("add if not existing").
    coding_agent_known_repos: str = "messaging-service,calendar-api,mobile-app"

    # ------------------------------------------------------------------
    # Claude Code coding agent
    # ------------------------------------------------------------------
    claude_code_oauth_token: str = ""
    claude_code_model: str = ""              # "" = CLI default
    claude_code_max_turns: int = 50
    claude_code_timeout_seconds: int = 1500  # wall clock for the claude subprocess
    claude_code_workspace_dir: str = "/workspaces"
    claude_code_keep_workspace: bool = False # debug: skip workspace cleanup

    # ------------------------------------------------------------------
    # Executive layer (morning brief + autopilot + ask)
    # ------------------------------------------------------------------
    # Daily morning brief (celery-beat crontab, UTC).
    morning_brief_enabled: bool = True
    morning_brief_cron: str = "0 8 * * *"
    # Comma-separated Telegram chat ids that receive the brief ("" = no push).
    telegram_brief_chat_id: str = ""
    # Delivery autopilot. The beat tick is ALWAYS scheduled; the runtime mode
    # (off/propose/auto) lives in app_settings so the dial flips without a
    # restart. These are the env-seeded defaults for that state.
    autopilot_tick_interval: int = 300          # seconds between ticks
    autopilot_default_mode: str = "auto"        # off | propose | auto
    autopilot_daily_limit: int = 5              # max auto-dispatches per day
    autopilot_stale_task_days: int = 3          # nudge in_progress older than this
    autopilot_blocker_escalation_days: int = 5  # escalate in_progress older than this
    autopilot_triage_per_tick: int = 3          # bound Groq triage calls per tick
    # /groom backlog hygiene: non-done task with no state change this long is stale.
    groom_stale_days: int = 14


# Singleton settings instance
settings = Settings()
