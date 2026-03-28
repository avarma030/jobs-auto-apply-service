from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.models import UserProfile


def load_profile(path: Path | str) -> UserProfile:
    """Load a UserProfile from a JSON file."""
    from src.models import UserProfile

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"User profile not found at {path}. "
            "Run `python main.py profile bootstrap --resume data/resume.pdf` to generate a draft, "
            "or copy data/user_profile.example.json to data/user_profile.json and fill in your details."
        )
    with path.open() as f:
        data = json.load(f)
    profile = UserProfile(**data)
    profile = _refresh_profile_from_resume_if_needed(profile, path)
    logger.info(f"Loaded profile for {profile.first_name} {profile.last_name}")
    return profile


def _refresh_profile_from_resume_if_needed(profile: UserProfile, profile_path: Path) -> UserProfile:
    resume_path = profile.resume_path
    if resume_path is None:
        return profile

    resolved_resume_path = Path(resume_path)
    if not resolved_resume_path.is_absolute():
        resolved_resume_path = (profile_path.parent.parent / resolved_resume_path).resolve()
        if not resolved_resume_path.exists():
            resolved_resume_path = (Path.cwd() / resume_path).resolve()

    if not resolved_resume_path.exists():
        return profile

    if not _profile_needs_refresh(profile):
        return profile

    try:
        from src.utils.profile_bootstrap import build_profile_from_resume_text, extract_resume_text

        refreshed = build_profile_from_resume_text(
            extract_resume_text(resolved_resume_path),
            resume_path=resolved_resume_path,
        ).profile
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.warning(f"Could not refresh stale profile data from resume: {exc}")
        return profile

    merged = _merge_profile(profile, refreshed, resolved_resume_path)
    if merged.model_dump(mode="json") == profile.model_dump(mode="json"):
        return merged

    with profile_path.open("w", encoding="utf-8") as handle:
        json.dump(merged.model_dump(mode="json"), handle, indent=2)
        handle.write("\n")

    logger.info(f"Refreshed stale profile data from resume at {resolved_resume_path}")
    return merged


def _profile_needs_refresh(profile: UserProfile) -> bool:
    return (
        not profile.work_experience
        or _skills_need_refresh(profile.skills)
        or _education_needs_refresh(profile)
    )


def _skills_need_refresh(skills: list[str]) -> bool:
    if not skills:
        return True

    noisy_entries = 0
    for skill in skills:
        cleaned = skill.strip()
        if not cleaned:
            noisy_entries += 1
            continue
        if cleaned in {"▪", "•", "■", "◆", "●", "◦", "â€¢", "â–ª"}:
            noisy_entries += 1
            continue
        if re.match(r"^[A-Za-z][A-Za-z &/+-]{2,25}:\s", cleaned):
            noisy_entries += 1
            continue

    return noisy_entries > 0


def _education_needs_refresh(profile: UserProfile) -> bool:
    for entry in profile.education:
        if entry.institution == entry.degree:
            return True
        if " – " in entry.institution or " - " in entry.institution:
            return True
    return False


def _merge_profile(profile: UserProfile, refreshed: UserProfile, resume_path: Path) -> UserProfile:
    merged = profile.model_copy(deep=True)
    merged.resume_path = resume_path

    if _skills_need_refresh(profile.skills) and refreshed.skills:
        merged.skills = refreshed.skills

    if not profile.work_experience and refreshed.work_experience:
        merged.work_experience = refreshed.work_experience

    if _education_needs_refresh(profile) and refreshed.education:
        merged.education = refreshed.education

    if profile.address is None and refreshed.address is not None:
        merged.address = refreshed.address

    if not profile.headline and refreshed.headline:
        merged.headline = refreshed.headline

    if not profile.summary and refreshed.summary:
        merged.summary = refreshed.summary

    if profile.years_of_experience is None and refreshed.years_of_experience is not None:
        merged.years_of_experience = refreshed.years_of_experience

    if not profile.languages and refreshed.languages:
        merged.languages = refreshed.languages

    merged.social_links = merged.social_links.model_copy(
        update={
            "linkedin": merged.social_links.linkedin or refreshed.social_links.linkedin,
            "github": merged.social_links.github or refreshed.social_links.github,
            "portfolio": merged.social_links.portfolio or refreshed.social_links.portfolio,
            "twitter": merged.social_links.twitter or refreshed.social_links.twitter,
        }
    )

    return merged
