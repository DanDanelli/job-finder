"""Hard filters: decide in code, without the LLM, whether a job stays in the pipeline.

The general rule is conservative: when the job lacks information (no salary, unknown
location), it passes, and the LLM scoring weighs it.
"""

from datetime import datetime, timedelta

from job_finder.domain import Filters, Job, Salary, WorkMode
from job_finder.pipeline.dedup import canonical_company
from job_finder.pipeline.locations import is_eligible
from job_finder.pipeline.salary import yearly
from job_finder.text import key

# Slack in the salary filter: a job up to 10% below the minimum still passes (the LLM
# weighs it).
SALARY_MARGIN = 0.10


def discard_reason(job: Job, filters: Filters, now: datetime) -> str | None:
    """Returns why the job is discarded, or None if it passes every filter."""
    if job.posted_at and now - job.posted_at > timedelta(days=filters.max_age_days):
        return "too_old"

    if (
        filters.work_modes
        and job.work_mode != WorkMode.UNKNOWN
        and job.work_mode not in filters.work_modes
    ):
        return "work_mode"

    if job.work_mode == WorkMode.REMOTE:
        if filters.remote_from and (
            is_eligible(job.eligible_locations, filters.remote_from) is False
        ):
            return "location"
    elif filters.countries and job.country and job.country not in filters.countries:
        return "country"

    if (
        filters.cities
        and job.city
        and job.work_mode != WorkMode.REMOTE
        and key(job.city) not in {key(c) for c in filters.cities}
    ):
        return "city"

    if (
        filters.contract_types
        and job.contract_type
        and (job.contract_type not in filters.contract_types)
    ):
        return "contract_type"

    if _below_salary(job, filters):
        return "salary"

    company = canonical_company(job.company)
    if company and company in {canonical_company(c) for c in filters.blocked_companies}:
        return "blocked_company"

    # Blocked keywords apply to the title only: in the description they give too many
    # false positives.
    title = f" {key(job.title)} "
    if any(f" {key(w)} " in title for w in filters.blocked_keywords if key(w)):
        return "blocked_keyword"

    return None


def min_salary_for(job: Job, filters: Filters) -> Salary | None:
    if job.contract_type and job.contract_type in filters.min_salary_by_contract:
        return filters.min_salary_by_contract[job.contract_type]
    return filters.min_salary


def _below_salary(job: Job, filters: Filters) -> bool:
    """Discards only when the comparison is safe: same currency, monthly or yearly salary
    on both sides, and the job's ceiling more than SALARY_MARGIN below the minimum.

    An hourly rate never discards here: the monthly equivalent depends on how many hours
    the job pays for, and the LLM weighs it with the posting's text.
    """
    minimum, salary = min_salary_for(job, filters), job.salary
    if not minimum or minimum.minimum is None or not salary:
        return False
    ceiling = salary.maximum if salary.maximum is not None else salary.minimum
    if ceiling is None or not salary.period or not salary.currency:
        return False
    if "hour" in (salary.period, minimum.period):
        return False
    if salary.currency != (minimum.currency or salary.currency):
        return False  # no currency conversion in phase 1
    floor = yearly(minimum.minimum, minimum.period or "year") * (1 - SALARY_MARGIN)
    return yearly(ceiling, salary.period) < floor
