"""Configuração da aplicação, lida de variáveis de ambiente e do arquivo `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
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
    # Conectores usados nas rodadas, ex: JOBFINDER_CONECTORES='["remotive"]'
    conectores: list[str] = ["remotive"]

    # Claude API. A chave usa o nome padrão do SDK, sem o prefixo JOBFINDER_.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "JOBFINDER_ANTHROPIC_API_KEY"),
    )
    modelo_perfil: str = "claude-opus-5-5"
    modelo_scoring: str = "claude-haiku-5-5"
    # Teto de gasto com o Claude no mês (UTC). Ao atingir, as chamadas param.
    custo_mensal_max_usd: float = Field(default=5.0, ge=0)
    # Quantas vagas pendentes, por perfil e rodada, vão para o LLM após o BM25.
    prerank_top_n: int = Field(default=30, ge=1)

    @property
    def resolved_database_url(self) -> str:
        """URL do banco; por padrão um SQLite dentro de `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'job_finder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
