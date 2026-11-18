from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    owner_id: int
    database_url: str
    hh_user_agent: str
    hh_access_token: str | None = None
    config_path: Path = Path("config/config.yaml")


@lru_cache
def get_settings() -> Settings:
    return Settings()
