"""Domain models shared by connectors, the pipeline, the LLM layer and the interfaces."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class WorkMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class ContractType(StrEnum):
    FULL_TIME = "full_time"  # in Brazil, CLT
    PART_TIME = "part_time"
    CONTRACT = "contract"  # contractor; in Brazil, PJ
    FREELANCE = "freelance"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class MatchStatus(StrEnum):
    PENDING = "pending"  # passed the filters, waiting for scoring (or scoring failed)
    NEW = "new"  # scored, no user action yet
    SAVED = "saved"
    DISMISSED = "dismissed"
    SKIPPED = "skipped"  # below the relevance cutoff; never sent to the LLM


SalaryPeriod = Literal["year", "month", "hour"]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]  # ISO 3166-1 alpha-2
# ISO alpha-2 country code or a region code (see pipeline/locations.py), e.g. "BR", "LATAM".
LocationCode = Annotated[str, Field(pattern=r"^[A-Z_]{2,}$")]


class Salary(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None  # ISO 4217, e.g. "BRL"
    period: SalaryPeriod | None = None


class RawJob(BaseModel):
    """A job posting as a connector delivers it, before normalization."""

    source: str
    source_id: str
    title: str
    company: str | None = None
    location: str | None = None  # free text, e.g. "LATAM, Europe" or "São Paulo, SP"
    description: str | None = None  # plain text (the connector strips the HTML)
    url: HttpUrl
    apply_url: HttpUrl | None = None
    posted_at: datetime | None = None
    work_mode: str | None = None  # source's free text, e.g. "remote", "hybrid"
    contract_type: str | None = None  # source's free text, e.g. "full_time"
    salary: Salary | None = None  # when the source gives it structured
    salary_text: str | None = None  # when the source gives text, e.g. "$35k - $52k"
    tags: list[str] = Field(default_factory=list)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    """A normalized, deduplicated job posting."""

    title: str
    company: str | None = None
    country: CountryCode | None = None
    city: str | None = None
    location_text: str | None = None
    # Countries/regions people can work from (remote jobs). Empty = unknown.
    eligible_locations: list[LocationCode] = Field(default_factory=list)
    work_mode: WorkMode = WorkMode.UNKNOWN
    contract_type: ContractType | None = None
    description: str | None = None
    salary: Salary | None = None
    posted_at: datetime | None = None
    url: HttpUrl
    apply_url: HttpUrl | None = None


class Skill(BaseModel):
    name: str
    weight: int = Field(default=3, ge=1, le=5)


class Language(BaseModel):
    code: str  # ISO 639-1, e.g. "pt"
    level: Literal["basic", "intermediate", "advanced", "fluent", "native"]


class Query(BaseModel):
    term: str
    countries: list[CountryCode] = Field(default_factory=list)  # empty = global
    active: bool = True


class Filters(BaseModel):
    """Hard filters, applied in code before any LLM call."""

    # Countries accepted for onsite/hybrid jobs. Empty = any.
    countries: list[CountryCode] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    # Countries the person can work remotely from (residence/work authorization).
    # A remote job passes if it is open to any of them. Empty = no filter.
    remote_from: list[CountryCode] = Field(default_factory=list)
    work_modes: list[WorkMode] = Field(default_factory=list)  # empty = all
    languages: list[str] = Field(default_factory=list)  # weighed by the LLM, not in code
    contract_types: list[ContractType] = Field(default_factory=list)  # empty = all
    # General minimum, plus minimums per contract type (e.g. full_time = CLT,
    # contract = PJ). A job with no contract type uses the general one.
    min_salary: Salary | None = None
    min_salary_by_contract: dict[ContractType, Salary] = Field(default_factory=dict)
    blocked_companies: list[str] = Field(default_factory=list)
    blocked_keywords: list[str] = Field(default_factory=list)
    max_age_days: int = Field(default=14, ge=1)


class ProfileData(BaseModel):
    """Professional profile extracted from the CV and reviewed by the user."""

    summary: str = ""
    roles: list[str] = Field(default_factory=list)
    seniority: str | None = None
    years_experience: int | None = Field(default=None, ge=0)
    skills: list[Skill] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    name: str
    telegram_chat_id: int | None = None
    data: ProfileData = Field(default_factory=ProfileData)
    queries: list[Query] = Field(default_factory=list)
    filters: Filters = Field(default_factory=Filters)
    preferences: str = ""


class FitResult(BaseModel):
    """Structured output of scoring one job for one profile."""

    score: int = Field(ge=0, le=100)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    summary: str
