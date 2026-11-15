"""Database tables (SQLAlchemy 2.x)."""

from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC and returns tzinfo=UTC (SQLite does not keep time zones)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use datetime.now(UTC)")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ProfileDB(Timestamps, Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # domain.ProfileData / Query / Filters serialized as JSON.
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    queries: Mapped[list[Any]] = mapped_column(default=list)
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    preferences: Mapped[str] = mapped_column(Text, default="")
    resume_path: Mapped[str | None] = mapped_column(String(512))
    # Paused: left out of scheduled runs and gets no digest.
    paused: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    # Site login (scrypt, see web/auth.py). None = cannot sign in to the site.
    password_hash: Mapped[str | None] = mapped_column(String(256))

    matches: Mapped[list["MatchDB"]] = relationship(back_populates="profile")


class JobDB(Timestamps, Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(512), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str | None] = mapped_column(String(256), index=True)
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    city: Mapped[str | None] = mapped_column(String(128))
    location_text: Mapped[str | None] = mapped_column(String(512))
    eligible_locations: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    work_mode: Mapped[str] = mapped_column(String(16))
    contract_type: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(8))
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    apply_url: Mapped[str | None] = mapped_column(String(2048))

    sources: Mapped[list["JobSourceDB"]] = relationship(back_populates="job")
    matches: Mapped[list["MatchDB"]] = relationship(back_populates="job")


class JobSourceDB(Base):
    """Each sighting of a job in a source (the same job can come from several)."""

    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    raw_data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    job: Mapped[JobDB] = relationship(back_populates="sources")


class MatchDB(Timestamps, Base):
    """Fit of one job for one profile. A pair is never scored twice."""

    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("job_id", "profile_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    prerank_score: Mapped[float | None] = mapped_column(Float)
    strengths: Mapped[list[Any]] = mapped_column(default=list)
    gaps: Mapped[list[Any]] = mapped_column(default=list)
    dealbreakers: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    job: Mapped[JobDB] = relationship(back_populates="matches")
    profile: Mapped[ProfileDB] = relationship(back_populates="matches")
    feedback: Mapped[list["FeedbackDB"]] = relationship(back_populates="match")


class FeedbackDB(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # save | dismiss
    origin: Mapped[str] = mapped_column(String(16))  # telegram | web
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    match: Mapped[MatchDB] = relationship(back_populates="feedback")


class RunDB(Base):
    """Log of each pipeline run."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))  # scheduled | on_demand | dry_run
    status: Mapped[str] = mapped_column(String(16))  # running | ok | partial | error
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    jobs_collected: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    matches_new: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[Any]] = mapped_column(default=list)


class QuotaUsageDB(Base):
    """Calls per connector per day; the monthly total is the sum of the days."""

    __tablename__ = "quota_usage"
    __table_args__ = (UniqueConstraint("connector", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connector: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class LLMCostDB(Base):
    """Tokens and cost of each Claude call, for the monthly cap and /status."""

    __tablename__ = "llm_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    operation: Mapped[str] = mapped_column(String(32))  # extract_profile | scoring | ...
    model: Mapped[str] = mapped_column(String(64))
    batch: Mapped[bool] = mapped_column(default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class SessionDB(Base):
    """A site login. The cookie carries the token; only its SHA-256 is stored."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
