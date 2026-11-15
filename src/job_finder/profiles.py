"""Profiles as YAML: the user reviews and edits what was extracted from the CV."""

from pathlib import Path

import yaml

from job_finder.domain import Profile


def read_profile_yaml(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("name", path.stem)
    return Profile.model_validate(data)


def write_profile_yaml(profile: Profile) -> str:
    """YAML in the format `read_profile_yaml` reads, to review and load back."""
    data = profile.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
