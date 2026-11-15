"""RawJob (each source's format) -> Job (a single format)."""

from datetime import UTC

from job_finder.domain import ContractType, Job, RawJob, WorkMode
from job_finder.pipeline.locations import WORLDWIDE, parse_location
from job_finder.pipeline.salary import parse_salary
from job_finder.text import key

# Terms in English and Portuguese: the sources and the users are in both.
_WORK_MODES = {
    WorkMode.REMOTE: {"remote", "remoto", "anywhere", "work from home", "home office", "wfh"},
    WorkMode.HYBRID: {"hybrid", "hibrido", "flexible"},
    WorkMode.ONSITE: {"onsite", "on site", "presencial", "in office", "office"},
}

_CONTRACTS = {
    ContractType.FULL_TIME: {
        "full time", "fulltime", "permanent", "tempo integral", "efetivo", "clt", "cdi",
    },
    ContractType.PART_TIME: {"part time", "parttime", "meio periodo"},
    ContractType.CONTRACT: {
        "contract", "contractor", "pj", "contrato", "prestador de servicos",
    },
    ContractType.FREELANCE: {"freelance", "freelancer"},
    ContractType.INTERNSHIP: {"internship", "intern", "estagio"},
    ContractType.TEMPORARY: {"temporary", "temp", "temporario"},
}  # fmt: skip


def _work_mode(text: str | None) -> WorkMode:
    if not text:
        return WorkMode.UNKNOWN
    k = key(text)
    for mode, terms in _WORK_MODES.items():
        if k in terms:
            return mode
    return WorkMode.UNKNOWN


def _contract_type(text: str | None) -> ContractType | None:
    if not text:
        return None
    k = key(text)
    for kind, terms in _CONTRACTS.items():
        if k in terms:
            return kind
    return None


def normalize(raw: RawJob) -> Job:
    locations = parse_location(raw.location)
    countries = [c for c in locations if len(c) == 2]

    posted = raw.posted_at
    if posted is not None and posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)

    return Job(
        title=" ".join(raw.title.split()),
        company=" ".join(raw.company.split()) if raw.company else None,
        # The job's country only when the location points to a single country.
        country=countries[0] if len(countries) == 1 and len(locations) == 1 else None,
        location_text=raw.location,
        eligible_locations=[WORLDWIDE] if WORLDWIDE in locations else locations,
        work_mode=_work_mode(raw.work_mode),
        contract_type=_contract_type(raw.contract_type),
        description=raw.description,
        salary=raw.salary or parse_salary(raw.salary_text),
        posted_at=posted,
        url=raw.url,
        apply_url=raw.apply_url,
    )
