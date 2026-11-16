"""Data access: conversion between tables and domain models."""

from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from job_finder.db.models import JobDB, JobSourceDB, MatchDB, ProfileDB, utcnow
from job_finder.domain import (
    ContractType,
    Filters,
    Job,
    Profile,
    ProfileData,
    Query,
    RawJob,
    Salary,
    WorkMode,
)
from job_finder.pipeline.dedup import is_fuzzy_duplicate, split_key

MANUAL_SOURCE = "manual"  # ad-hoc evaluation requested through the bot

# --- profiles -----------------------------------------------------------------


def save_profile(s: Session, profile: Profile) -> ProfileDB:
    """Creates or updates the profile by name."""
    db = find_profile(s, profile.name)
    if db is None:
        db = ProfileDB(name=profile.name)
        s.add(db)
    db.telegram_chat_id = profile.telegram_chat_id
    db.data = profile.data.model_dump(mode="json")
    db.queries = [q.model_dump(mode="json") for q in profile.queries]
    db.filters = profile.filters.model_dump(mode="json")
    db.preferences = profile.preferences
    s.flush()
    return db


def find_profile(s: Session, name: str) -> ProfileDB | None:
    return s.scalar(select(ProfileDB).where(ProfileDB.name == name))


def profile_from(db: ProfileDB) -> Profile:
    return Profile(
        name=db.name,
        telegram_chat_id=db.telegram_chat_id,
        data=ProfileData.model_validate(db.data or {}),
        queries=[Query.model_validate(q) for q in db.queries or []],
        filters=Filters.model_validate(db.filters or {}),
        preferences=db.preferences,
        learned_rules=list(db.learned_rules or []),
    )


def list_profiles(
    s: Session, names: list[str] | None = None, *, include_paused: bool = True
) -> list[ProfileDB]:
    query = select(ProfileDB).order_by(ProfileDB.name)
    if names:
        query = query.where(ProfileDB.name.in_(names))
    if not include_paused:
        query = query.where(ProfileDB.paused.is_(False))
    return list(s.scalars(query))


def profile_for_chat(s: Session, chat_id: int) -> ProfileDB | None:
    return s.scalar(select(ProfileDB).where(ProfileDB.telegram_chat_id == chat_id))


# --- jobs ---------------------------------------------------------------------


# How far back the fuzzy match looks for the same job.
FUZZY_DAYS = 30


def record_job(s: Session, raw: RawJob, job: Job, key: str) -> tuple[JobDB, bool]:
    """Stores the job (or reuses the existing one, same key or fuzzy match) and its source
    sighting.

    Returns (job, is_new), where `is_new` means the job was not in the database yet.
    """
    source = s.scalar(
        select(JobSourceDB).where(
            JobSourceDB.source == raw.source, JobSourceDB.source_id == raw.source_id
        )
    )
    if source is not None:
        return source.job, False

    db = s.scalar(select(JobDB).where(JobDB.dedup_key == key)) or _similar_job(s, key)
    is_new = db is None
    if db is None:
        db = _job_db(job, key)
        s.add(db)
        s.flush()
    elif db.apply_url is None and job.apply_url is not None:
        db.apply_url = str(job.apply_url)  # prefer the direct application link
    s.add(
        JobSourceDB(
            job_id=db.id,
            source=raw.source,
            source_id=raw.source_id,
            url=str(raw.url),
            raw_data=raw.raw_data,
        )
    )
    s.flush()
    return db, is_new


def _similar_job(s: Session, key: str) -> JobDB | None:
    company, _, _ = split_key(key)
    if not company:
        return None
    prefix = company.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = s.scalars(
        select(JobDB)
        .where(
            JobDB.dedup_key.like(f"{prefix}|%", escape="\\"),
            JobDB.created_at >= utcnow() - timedelta(days=FUZZY_DAYS),
        )
        .order_by(JobDB.id)
    )
    return next((c for c in candidates if is_fuzzy_duplicate(key, c.dedup_key)), None)


def _job_db(job: Job, key: str) -> JobDB:
    salary = job.salary or Salary()
    return JobDB(
        dedup_key=key,
        title=job.title,
        company=job.company,
        country=job.country,
        city=job.city,
        location_text=job.location_text,
        eligible_locations=list(job.eligible_locations),
        work_mode=job.work_mode.value,
        contract_type=job.contract_type.value if job.contract_type else None,
        description=job.description,
        salary_min=salary.minimum,
        salary_max=salary.maximum,
        salary_currency=salary.currency,
        salary_period=salary.period,
        posted_at=job.posted_at,
        url=str(job.url),
        apply_url=str(job.apply_url) if job.apply_url else None,
    )


def job_from(db: JobDB) -> Job:
    has_salary = db.salary_min is not None or db.salary_max is not None
    return Job(
        title=db.title,
        company=db.company,
        country=db.country,
        city=db.city,
        location_text=db.location_text,
        eligible_locations=db.eligible_locations or [],
        work_mode=WorkMode(db.work_mode),
        contract_type=ContractType(db.contract_type) if db.contract_type else None,
        description=db.description,
        salary=Salary(
            minimum=db.salary_min,
            maximum=db.salary_max,
            currency=db.salary_currency,
            period=db.salary_period,
        )
        if has_salary
        else None,
        posted_at=db.posted_at,
        url=db.url,
        apply_url=db.apply_url,
    )


def candidates(s: Session, profile_id: int, since: datetime) -> list[JobDB]:
    """Recent jobs that have no match for the profile yet.

    Jobs that only arrived through an ad-hoc evaluation (source "manual") are left out:
    they belong to whoever asked and do not enter the other profiles' runs.
    """
    no_match = ~exists().where(and_(MatchDB.job_id == JobDB.id, MatchDB.profile_id == profile_id))
    from_some_source = exists().where(
        and_(JobSourceDB.job_id == JobDB.id, JobSourceDB.source != MANUAL_SOURCE)
    )
    recent = or_(
        JobDB.posted_at >= since,
        and_(JobDB.posted_at.is_(None), JobDB.created_at >= since),
    )
    return list(
        s.scalars(select(JobDB).where(no_match, recent, from_some_source).order_by(JobDB.id))
    )
