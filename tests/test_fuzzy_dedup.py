"""Fuzzy deduplication: same company, compatible place, nearly the same title."""

from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from job_finder.db import repo
from job_finder.db.models import JobDB, JobSourceDB, utcnow
from job_finder.db.session import session
from job_finder.domain import RawJob
from job_finder.pipeline.dedup import dedup_key, is_fuzzy_duplicate
from job_finder.pipeline.normalize import normalize


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        # The real case: one source says only "São Paulo", so the country is unknown.
        (
            "jobbol|analista de eventos e marketing senior ibracon|?",
            "jobbol|analista de eventos e marketing senior ibracon|br",
            True,
        ),
        (
            "adaptive teams|social media community manager latam remote|remote",
            "adaptive teams|social media community manager|remote",
            True,
        ),
        ("acme|cloud architect aws|br", "acme|aws cloud architect|br", True),  # word order
        ("acme|data engineer|br", "acme|senior data engineer|br", False),  # seniority
        ("lemon io|senior data engineer|remote", "lemon io|senior devops engineer|remote", False),
        ("acme|cloud architect|br", "acme|cloud architect|pt", False),  # other country
        ("acme|cloud architect|br", "beta|cloud architect|br", False),  # other company
        ("|cloud architect|br", "|cloud architect|br", False),  # no company: no fuzzy match
        ("acme|remote|br", "acme|remote latam|br", False),  # nothing left to compare
    ],
)
def test_is_fuzzy_duplicate(a: str, b: str, same: bool) -> None:
    assert is_fuzzy_duplicate(a, b) is same


def raw(source: str, source_id: str, title: str, location: str, **kw: object) -> RawJob:
    return RawJob.model_validate(
        {
            "source": source,
            "source_id": source_id,
            "title": title,
            "company": "Jobbol",
            "location": location,
            "url": f"https://{source}.example/{source_id}",
            **kw,
        }
    )


def record(engine: Engine, r: RawJob) -> tuple[int, bool]:
    with session(engine) as s:
        job = normalize(r)
        db, is_new = repo.record_job(s, r, job, dedup_key(job))
        return db.id, is_new


def test_record_job_merges_a_fuzzy_duplicate(engine: Engine) -> None:
    first, new1 = record(engine, raw("jsearch", "1", "Analista de Eventos (Ibracon)", "São Paulo"))
    second, new2 = record(
        engine,
        raw(
            "adzuna",
            "2",
            "Analista de Eventos (Ibracon)",
            "Osasco, SP, BR",
            apply_url="https://jobbol.example/apply/2",
        ),
    )

    assert (new1, new2) == (True, False) and first == second
    with session(engine) as s:
        job = s.get_one(JobDB, first)
        assert sorted(x.source for x in job.sources) == ["adzuna", "jsearch"]
        assert job.apply_url == "https://jobbol.example/apply/2"  # direct link kept


def test_old_jobs_are_not_fuzzy_matched(engine: Engine) -> None:
    first, _ = record(engine, raw("jsearch", "1", "Analista de Eventos", "São Paulo"))
    with session(engine) as s:
        s.get_one(JobDB, first).created_at = utcnow() - timedelta(days=repo.FUZZY_DAYS + 1)

    second, is_new = record(engine, raw("adzuna", "2", "Analista de Eventos", "Osasco, SP, BR"))

    assert is_new and second != first
    with session(engine) as s:
        assert len(s.scalars(select(JobSourceDB)).all()) == 2
