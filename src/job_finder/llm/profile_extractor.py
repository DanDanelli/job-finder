"""CV as PDF -> ProfileData + suggested search terms (Claude Opus 5.5)."""

import base64
from pathlib import Path

from pydantic import BaseModel, Field

from job_finder.domain import ProfileData
from job_finder.i18n import language_name
from job_finder.llm.client import LLMClient

OPERATION = "extract_profile"

INSTRUCTIONS = """\
You analyze CVs for a personal job search assistant. From the attached CV, extract a \
professional profile faithful to the document. Do not make up anything that is not \
written in it or cannot be directly inferred from it.

Fields:
- summary: 2 to 4 sentences, in {language}, about who the person is professionally, what \
they do best and in which kind of role they perform best.
- roles: job titles the person has held or is ready for, as they appear in job postings \
(in English when the market uses the English term).
- seniority: one of junior, mid, senior, lead, principal, manager, director.
- years_experience: total years of relevant professional experience, an integer.
- skills: technical and business skills, weighted 1 to 5 by the evidence in the CV \
(5 = central and recent in the career, 1 = mentioned in passing). At most 30, from most \
to least important. Use the canonical name (e.g. "AWS", "Terraform", "SEO").
- languages: ISO 639-1 code and level (basic, intermediate, advanced, fluent, native).
- industries: market sectors the person has worked in (e.g. energy, retail, fashion).
- certifications: formal certifications, with their official name.
- search_terms: 3 to 8 short terms to search for jobs on job sites, from best to worst \
fit. Mix exact titles and common market variations.
"""


class ProfileExtraction(BaseModel):
    data: ProfileData
    search_terms: list[str] = Field(default_factory=list)


def extract_profile(
    client: LLMClient,
    pdf: Path,
    *,
    model: str,
    language: str,
    profile_id: int | None = None,
) -> ProfileExtraction:
    content = base64.standard_b64encode(pdf.read_bytes()).decode("ascii")
    return client.structured(
        ProfileExtraction,
        operation=OPERATION,
        model=model,
        profile_id=profile_id,
        max_tokens=16_000,
        # A single, cheap task; high effort so the extraction comes out complete.
        output_config={"effort": "high"},
        system=INSTRUCTIONS.format(language=language_name(language)),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": content,
                        },
                    },
                    {"type": "text", "text": "Extract the profile from this CV."},
                ],
            }
        ],
    )
