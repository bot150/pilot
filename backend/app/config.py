from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "IncidentPilot API"
    app_env: str = "development"
    database_url: str = "sqlite:///./incidentpilot.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"
    hindsight_api_url: str | None = None
    hindsight_api_key: str | None = None
    hindsight_bank_id: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-6-astra"

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
