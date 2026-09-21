from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets are supplied only through environment variables."""

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", populate_by_name=True)

    database_url: str = Field(validation_alias="DX_DATABASE_URL")
    root_api_key: str = Field(validation_alias="DX_ROOT_API_KEY")
    control_ui_origins: str = Field(
        default="http://127.0.0.1:5173,http://localhost:5173",
        validation_alias="DX_CONTROL_UI_ORIGINS",
    )
    actor_type: str = "human"
    actor_id: str = "dx"
    deepseek_api_key: str | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", validation_alias="DEEPSEEK_MODEL")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com", validation_alias="ANTHROPIC_BASE_URL"
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514", validation_alias="ANTHROPIC_MODEL"
    )
    llm_planner_provider: str = Field(
        default="deepseek", validation_alias="DX_LLM_PLANNER_PROVIDER"
    )
    llm_reviewer_provider: str = Field(
        default="anthropic", validation_alias="DX_LLM_REVIEWER_PROVIDER"
    )
    llm_fallback_provider: str | None = Field(
        default=None, validation_alias="DX_LLM_FALLBACK_PROVIDER"
    )
    llm_live_enabled: bool = Field(default=False, validation_alias="DX_LLM_LIVE_ENABLED")
    llm_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, validation_alias="DX_LLM_TIMEOUT_SECONDS"
    )
    llm_max_retries: int = Field(
        default=0, ge=0, le=3, validation_alias="DX_LLM_MAX_RETRIES"
    )
    # Phase E.3A safety foundation. Exactly two new settings, and nothing about
    # provider selection, timeouts or subprocess behaviour -- those belong to E.3B.
    #
    # agent_execution_enabled: the master switch. Absent -> False. A value that is not a
    # boolean raises a normal configuration validation error; a malformed value is never
    # silently coerced to False.
    # agent_workspaces: the static symbolic workspace map consumed by WorkspaceResolver
    # (``workspace_ref`` -> absolute directory). Absent -> empty map -> every reference is
    # unknown and therefore denied.
    agent_execution_enabled: bool = Field(
        default=False, validation_alias="DX_AGENT_EXECUTION_ENABLED"
    )
    agent_workspaces: dict[str, str] = Field(
        default_factory=dict, validation_alias="DX_AGENT_WORKSPACES"
    )

    @property
    def control_ui_origin_list(self) -> list[str]:
        """Return explicitly configured browser origins for the read-only UI."""

        return [origin.strip() for origin in self.control_ui_origins.split(",") if origin.strip()]


class WorkerSettings(BaseSettings):
    """Configuration for the internal outbox worker's distinct database role.

    Two principals, never mixed:

    * ``DX_WORKER_DATABASE_URL``     -> OutboxWorker, authenticated as ``dx_worker``.
    * ``DX_EXECUTION_DATABASE_URL``  -> Execution Gateway, authenticated as ``dx_api``.

    The second is optional and has NO fallback: when it is absent the Execution Gateway
    is not wired at all and the worker keeps its original behaviour. The worker process
    therefore never needs ``DX_ROOT_API_KEY``, and no GRANT is added to ``dx_worker``.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    database_url: str = Field(validation_alias="DX_WORKER_DATABASE_URL")
    execution_database_url: str | None = Field(
        default=None, validation_alias="DX_EXECUTION_DATABASE_URL"
    )
