"""Configuração da aplicação, lida de variáveis de ambiente e do arquivo `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JOBFINDER_",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    database_url: str | None = None
    timezone: str = "Europe/Lisbon"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def resolved_database_url(self) -> str:
        """URL do banco; por padrão um SQLite dentro de `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'job_finder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
