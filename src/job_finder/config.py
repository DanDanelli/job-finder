"""Configuração da aplicação, lida de variáveis de ambiente e do arquivo `.env`."""

from datetime import time
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
    timezone: str = "America/Sao_Paulo"
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

    # Telegram: token do @BotFather. Sem token, o bot não sobe (as rodadas seguem).
    telegram_bot_token: SecretStr | None = None
    # Rodadas agendadas, no fuso `timezone`, ex: JOBFINDER_HORARIOS='["08:00","18:00"]'
    agendamento_ativo: bool = True
    horarios: list[time] = [time(8), time(18)]
    # Digest: vagas com score >= mínimo, no máximo N por envio (o resto fica para o próximo).
    digest_score_minimo: int = Field(default=60, ge=0, le=100)
    digest_max_vagas: int = Field(default=10, ge=1)

    # JSearch (Google for Jobs), chave de app.openwebninja.com.
    # Ative com JOBFINDER_CONECTORES='["remotive","jsearch"]'.
    jsearch_api_key: SecretStr | None = None
    jsearch_paises: list[str] = ["br"]  # para queries sem país
    jsearch_buscas_por_rodada: int = Field(default=3, ge=1)
    jsearch_limite_diario: int | None = 6
    jsearch_limite_mensal: int | None = 190  # plano grátis: ~200/mês, com folga
    jsearch_publicadas_em: str = "3days"  # all, today, 3days, week, month

    @property
    def resolved_database_url(self) -> str:
        """URL do banco; por padrão um SQLite dentro de `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'job_finder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
