from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
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

    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
