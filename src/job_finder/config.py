"""Application settings, read from environment variables and the `.env` file."""

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from job_finder.pipeline.prerank import RelevanceCutoff

Language = Literal["en", "pt-BR"]


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
    # Language of the bot's messages and of the text Claude writes (summaries, strengths).
    language: Language = "en"
    # Connectors used in runs, e.g. JOBFINDER_CONNECTORS='["remotive"]'
    connectors: list[str] = ["remotive"]

    # Claude API. The key uses the SDK's standard name, without the JOBFINDER_ prefix.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "JOBFINDER_ANTHROPIC_API_KEY"),
    )
    profile_model: str = "claude-opus-5-5"
    scoring_model: str = "claude-haiku-5-5"
    # Monthly spend cap for Claude (UTC month). Once reached, calls stop.
    monthly_cost_cap_usd: float = Field(default=5.0, ge=0)
    # How many pending jobs, per profile and run, go to the LLM after BM25.
    prerank_top_n: int = Field(default=30, ge=1)
    # Relevance cutoff before the LLM (see pipeline/prerank.py): a job goes on if this
    # share of the profile's terms is in its title, or the second share in its whole
    # text. Set both to 0 to send everything.
    relevance_min_title: float = Field(default=0.02, ge=0, le=1)
    relevance_min_text: float = Field(default=0.14, ge=0, le=1)

    @property
    def relevance_cutoff(self) -> RelevanceCutoff:
        return RelevanceCutoff(self.relevance_min_title, self.relevance_min_text)

    # Telegram: token from @BotFather. Without a token the bot does not start (runs go on).
    telegram_bot_token: SecretStr | None = None
    # Scheduled runs, in `timezone`, e.g. JOBFINDER_RUN_TIMES='["08:00","18:00"]'
    schedule_enabled: bool = True
    run_times: list[time] = [time(8), time(18)]
    # Digest: jobs with score >= minimum, at most N per message (the rest waits for the next).
    digest_min_score: int = Field(default=60, ge=0, le=100)
    digest_max_jobs: int = Field(default=10, ge=1)

    # JSearch (Google for Jobs), key from app.openwebninja.com.
    # Enable with JOBFINDER_CONNECTORS='["remotive","jsearch"]'.
    jsearch_api_key: SecretStr | None = None
    jsearch_countries: list[str] = ["br"]  # for queries with no country
    jsearch_searches_per_run: int = Field(default=3, ge=1)
    jsearch_daily_limit: int | None = 6
    jsearch_monthly_limit: int | None = 190  # free plan: ~200/month, with some slack
    jsearch_date_posted: str = "3days"  # all, today, 3days, week, month

    # Himalayas (remote jobs). The key is optional: without it, calls share a public
    # allowance; a free key from himalayas.app raises the limit.
    himalayas_api_key: SecretStr | None = None
    himalayas_countries: list[str] = ["BR"]  # for queries with no country
    himalayas_searches_per_run: int = Field(default=5, ge=1)
    himalayas_daily_limit: int | None = 20

    # Adzuna, keys from developer.adzuna.com (both required).
    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None
    adzuna_countries: list[str] = ["br"]  # for queries with no country
    adzuna_searches_per_run: int = Field(default=4, ge=1)
    adzuna_daily_limit: int | None = 10
    adzuna_monthly_limit: int | None = 250
    adzuna_max_days_old: int = Field(default=7, ge=1)

    # We Work Remotely (public RSS feeds, no key): one request per category feed per run.
    # The names are the feeds' slugs (weworkremotely.com/categories/<slug>.rss).
    weworkremotely_categories: list[str] = [
        "remote-back-end-programming-jobs",
        "remote-front-end-programming-jobs",
        "remote-full-stack-programming-jobs",
        "remote-programming-jobs",
        "remote-devops-sysadmin-jobs",
        "remote-design-jobs",
        "remote-product-jobs",
        "remote-customer-support-jobs",
        "remote-sales-and-marketing-jobs",
        "remote-management-and-finance-jobs",
        "all-other-remote-jobs",
    ]
    weworkremotely_daily_limit: int | None = 33  # three full runs

    # Site. The cookie is Secure by default (tailscale serve gives HTTPS); turn it off
    # only for plain-HTTP access on a trusted network.
    web_secure_cookie: bool = True
    web_session_days: int = Field(default=30, ge=1)

    # Learned rules (llm/learner.py): after a scheduled run, when a profile has at least
    # `learning_min_decisions` saved/dismissed jobs and new ones since Claude last
    # looked, at most once every `learning_interval_days`. Approved on the site.
    learning_enabled: bool = True
    learning_model: str = "claude-opus-5-5"
    learning_interval_days: int = Field(default=7, ge=1)
    learning_min_decisions: int = Field(default=5, ge=1)
    # The site's address, for links in Telegram messages (optional).
    site_url: str | None = None

    # Daily SQLite backup at `backup_time` (in `timezone`) and on start if today's copy
    # is missing. Point `backup_dir` at another disk than the database.
    backup_enabled: bool = True
    backup_dir: Path | None = None  # default: <data_dir>/backup
    backup_time: time = time(3, 30)
    backup_keep_days: int = Field(default=7, ge=1)

    @property
    def resolved_backup_dir(self) -> Path:
        return self.backup_dir or self.data_dir / "backup"

    @property
    def resolved_database_url(self) -> str:
        """Database URL; by default a SQLite file inside `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'job_finder.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
