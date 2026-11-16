"""CV on the site: upload -> extraction by Claude (in the background) -> review -> save.

The uploaded PDF and what Claude extracted wait under `data/resumes/pending/` until the
person reviews and saves them; only then does the PDF replace `data/resumes/<name>.pdf`
and the extracted data replace the profile's. Discarding deletes both. Nothing from an
upload reaches the profile without that review.
"""

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_finder.domain import Language, ProfileData, Skill
from job_finder.llm.profile_extractor import ProfileExtraction

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 10 * 1024 * 1024
SENIORITIES = ("junior", "mid", "senior", "lead", "principal", "manager", "director")
LEVELS = ("basic", "intermediate", "advanced", "fluent", "native")
# Also accepted when typed in Portuguese.
_LEVEL_ALIASES = {
    "básico": "basic",
    "basico": "basic",
    "intermediário": "intermediate",
    "intermediario": "intermediate",
    "avançado": "advanced",
    "avancado": "advanced",
    "fluente": "fluent",
    "nativo": "native",
}

# (pdf, profile_id) -> what Claude read. Runs in a worker thread.
Extractor = Callable[[Path, int], ProfileExtraction]


def is_pdf(content: bytes) -> bool:
    return content.startswith(b"%PDF-")


class CVStore:
    """Files of each profile's CV. Pending files are named by profile id."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "resumes"
        self.pending = self.root / "pending"

    def upload_path(self, profile_id: int) -> Path:
        return self.pending / f"{profile_id}.pdf"

    def draft_path(self, profile_id: int) -> Path:
        return self.pending / f"{profile_id}.json"

    def final_path(self, profile_id: int) -> Path:
        # By id, so a name never becomes part of a path (the CLI's `profile import` uses
        # <name>.pdf); the profile's `resume_path` records which file is current.
        return self.root / f"{profile_id}.pdf"

    def save_upload(self, profile_id: int, content: bytes) -> Path:
        self.pending.mkdir(parents=True, exist_ok=True)
        self.draft_path(profile_id).unlink(missing_ok=True)
        path = self.upload_path(profile_id)
        path.write_bytes(content)
        os.chmod(path, 0o600)
        return path

    def save_draft(self, profile_id: int, extraction: ProfileExtraction) -> None:
        path = self.draft_path(profile_id)
        path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(path, 0o600)

    def draft(self, profile_id: int) -> ProfileExtraction | None:
        path = self.draft_path(profile_id)
        if not path.exists() or not self.upload_path(profile_id).exists():
            return None
        try:
            return ProfileExtraction.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("unreadable CV draft for profile %d; discarding it", profile_id)
            self.discard(profile_id)
            return None

    def discard(self, profile_id: int) -> None:
        self.upload_path(profile_id).unlink(missing_ok=True)
        self.draft_path(profile_id).unlink(missing_ok=True)

    def promote(self, profile_id: int) -> Path:
        """Moves the reviewed upload to the profile's CV and drops the draft."""
        target = self.final_path(profile_id)
        self.upload_path(profile_id).replace(target)
        self.draft_path(profile_id).unlink(missing_ok=True)
        return target


@dataclass
class Extraction:
    started_at: datetime
    error: str | None = None
    task: "asyncio.Task[None] | None" = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class Extractions:
    """At most one extraction per profile, in the background, in memory."""

    def __init__(self, store: CVStore, extract: Extractor) -> None:
        self.store = store
        self.extract = extract
        self._by_profile: dict[int, Extraction] = {}

    def get(self, profile_id: int) -> Extraction | None:
        return self._by_profile.get(profile_id)

    def clear(self, profile_id: int) -> None:
        job = self._by_profile.get(profile_id)
        if job is not None and not job.running:
            del self._by_profile[profile_id]

    def start(self, profile_id: int) -> bool:
        """Starts reading the uploaded PDF. Must be called from the event loop."""
        job = self._by_profile.get(profile_id)
        if job is not None and job.running:
            return False
        job = Extraction(datetime.now(UTC))
        job.task = asyncio.create_task(self._run(profile_id, job), name=f"cv {profile_id}")
        self._by_profile[profile_id] = job
        return True

    async def _run(self, profile_id: int, job: Extraction) -> None:
        pdf = self.store.upload_path(profile_id)
        try:
            extraction = await asyncio.to_thread(self.extract, pdf, profile_id)
            self.store.save_draft(profile_id, extraction)
        except Exception as e:
            log.exception("CV extraction failed for profile %d", profile_id)
            job.error = f"{type(e).__name__}: {e}"
            self.store.discard(profile_id)


# --- review form ------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line:
            seen.setdefault(line.casefold(), line)
    return list(seen.values())


def data_values(data: ProfileData, level_label: Callable[[str], str]) -> dict[str, str]:
    return {
        "summary": data.summary,
        "roles": "\n".join(data.roles),
        "seniority": data.seniority or "",
        "years_experience": "" if data.years_experience is None else str(data.years_experience),
        "skills": "\n".join(f"{k.name} | {k.weight}" for k in data.skills),
        "languages": "\n".join(f"{x.code} | {level_label(x.level)}" for x in data.languages),
        "industries": "\n".join(data.industries),
        "certifications": "\n".join(data.certifications),
    }


@dataclass(frozen=True)
class ParsedData:
    data: ProfileData
    errors: dict[str, str]


def parse_data(form: Mapping[str, str], level_codes: Mapping[str, str]) -> ParsedData:
    """Reads the review form. `level_codes` maps each level's label in the site's
    language (lowercase) to its code, so either the label or the code is accepted."""
    errors: dict[str, str] = {}

    seniority = form.get("seniority", "").strip().lower() or None
    if seniority is not None and seniority not in SENIORITIES:
        errors["seniority"] = seniority
        seniority = None

    years_text = form.get("years_experience", "").strip()
    years: int | None = None
    if years_text:
        if years_text.isdigit() and int(years_text) <= 80:
            years = int(years_text)
        else:
            errors["years_experience"] = years_text

    skills: list[Skill] = []
    bad_skills: list[str] = []
    for line in _lines(form.get("skills", "")):
        name, _, weight = (part.strip() for part in line.partition("|"))
        if not name or name.casefold() in {k.name.casefold() for k in skills}:
            continue  # the first mention of a skill wins
        if not weight:
            skills.append(Skill(name=name))
        elif weight.isdigit() and 1 <= int(weight) <= 5:
            skills.append(Skill(name=name, weight=int(weight)))
        else:
            bad_skills.append(line)
    if bad_skills:
        errors["skills"] = ", ".join(bad_skills)

    languages: list[Language] = []
    bad_languages: list[str] = []
    for line in _lines(form.get("languages", "")):
        code, _, level = (part.strip() for part in line.partition("|"))
        level = level.lower()
        level = level_codes.get(level) or _LEVEL_ALIASES.get(level) or level
        if code.lower() in {x.code for x in languages}:
            continue
        if code and len(code) <= 8 and level in LEVELS:
            languages.append(Language.model_validate({"code": code.lower(), "level": level}))
        else:
            bad_languages.append(line)
    if bad_languages:
        errors["languages"] = ", ".join(bad_languages)

    data = ProfileData(
        summary=form.get("summary", "").strip(),
        roles=_lines(form.get("roles", "")),
        seniority=seniority,
        years_experience=years,
        skills=skills,
        languages=languages,
        industries=_lines(form.get("industries", "")),
        certifications=_lines(form.get("certifications", "")),
    )
    return ParsedData(data, errors)
