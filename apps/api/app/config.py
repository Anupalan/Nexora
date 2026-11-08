from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    # Client context on request records. IP addresses are personal data: turn off if you do not need them.
    log_client_ip: bool = True
    log_sink_dir: str | None = None  # directory file sinks may write into; file sinks are refused without it
    trust_forwarded_for: bool = False  # only behind a reverse proxy you control that sets X-Forwarded-For
    app_name: str = "Nexora"
    public_app_url: str = "http://localhost:3000"

    database_url: str = "postgresql+psycopg://nexora:nexora@localhost:5432/nexora"
    redis_url: str = "redis://localhost:6379/0"

    # Sessions
    session_cookie_name: str = "nexora_session"
    csrf_cookie_name: str = "nexora_csrf"
    session_idle_minutes: int = 60 * 8
    session_absolute_hours: int = 24 * 7
    cookie_secure: bool = True

    # Login protection
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # LiteLLM gateway (private network). The master key is server-side only.
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = ""
    gateway_timeout_seconds: float = 600.0
    key_prefix: str = "sk-nx-"

    # Scheduler / quotas (roadmap §5.5, §5.6, §21)
    default_max_active_requests: int = 2  # per model deployment; raise after benchmarking
    default_max_queue_length: int = 100
    queue_timeout_seconds: int = 300
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    waiter_ttl_seconds: int = 15
    default_output_tokens_estimate: int = 1024
    sweeper_interval_seconds: int = 10

    # Operations: health checks, alerts, metrics (roadmap §5.9, §30)
    ops_loop_enabled: bool = True
    health_check_interval_seconds: int = 30
    health_timeout_seconds: float = 10.0
    alert_queue_depth: int = 20
    alert_queue_wait_ms: int = 30000
    alert_quota_percent: int = 90
    alert_auth_failures: int = 10  # failed logins in 10 minutes
    metrics_token: str = ""  # /metrics is disabled (404) unless a bearer token is configured

    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
