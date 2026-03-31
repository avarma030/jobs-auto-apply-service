from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.models import UserProfile
from src.models.user_profile import SocialLinks, WorkExperience
from src.services.ai_matcher import _local_match_evidence, score_compatibility


class FakeMatchCacheBackend:
    def __init__(self):
        self.cache: dict[str, tuple[str, dict]] = {}
        self.examples: list[SimpleNamespace] = []

    async def get_semantic_cache(self, key: str, source_hash: str | None = None) -> dict | None:
        cached = self.cache.get(key)
        if cached is None:
            return None
        cached_hash, payload = cached
        if source_hash is not None and cached_hash != source_hash:
            return None
        return payload

    async def upsert_semantic_cache(
        self,
        key: str,
        *,
        kind: str,
        source_hash: str,
        payload: dict,
        user_id: int | None = None,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.cache[key] = (source_hash, dict(payload))

    async def get_recent_match_examples(
        self,
        user_id: int,
        *,
        limit: int = 12,
        exclude_job_id: int | None = None,
    ) -> list[SimpleNamespace]:
        return [record for record in self.examples if record.id != exclude_job_id][:limit]


def make_profile() -> UserProfile:
    return UserProfile(
        first_name="Rucha",
        last_name="Varma",
        email="rucha@example.com",
        headline="IT Project Manager | Agile Delivery Lead",
        summary="Project and delivery leader with strong stakeholder management experience.",
        years_of_experience=8,
        skills=[
            "Agile Delivery",
            "Stakeholder Management",
            "Project Delivery",
            "Scrum",
            "Budget Management",
        ],
        work_experience=[
            WorkExperience(
                company="Example Corp",
                title="Technical Program Manager",
                start_date="2021-01",
                end_date="2024-12",
                description="Owned cross-functional delivery and stakeholder communication.",
            ),
            WorkExperience(
                company="Example Corp",
                title="Project Manager",
                start_date="2018-01",
                end_date="2020-12",
                description="Managed delivery plans, budgets, and project stakeholders.",
            ),
        ],
        social_links=SocialLinks(),
    )


def make_client(payload: dict[str, object]) -> AsyncMock:
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])
    )
    return client


def test_local_match_evidence_rejects_obvious_mismatch():
    candidate_fp = {
        "titles": ["it project manager", "technical program manager"],
        "skills": ["agile delivery", "stakeholder management", "project delivery"],
        "domains": ["software delivery"],
        "seniority": "senior",
        "snapshot": "candidate",
    }
    job_fp = {
        "titles": ["clinical psychologist"],
        "required_skills": ["clinical assessment", "psychology degree", "therapy practice"],
        "preferred_skills": [],
        "domains": ["mental health"],
        "hard_blockers": ["registered psychologist"],
        "seniority": "senior",
    }

    evidence = _local_match_evidence(candidate_fp, job_fp, ["project manager"])

    assert evidence["local_pre_score"] < 18
    assert evidence["role_alignment"] < 0.18
    assert evidence["required_skill_overlap"] < 0.12


def test_local_match_evidence_keeps_borderline_project_manager_role_alive():
    candidate_fp = {
        "titles": ["it project manager", "technical program manager"],
        "skills": ["agile delivery", "stakeholder management", "project delivery", "budget management"],
        "domains": ["software delivery", "digital transformation"],
        "seniority": "senior",
        "snapshot": "candidate",
    }
    job_fp = {
        "titles": ["senior project manager"],
        "required_skills": ["stakeholder management", "project delivery", "budget ownership"],
        "preferred_skills": ["capital delivery"],
        "domains": ["infrastructure delivery"],
        "hard_blockers": [],
        "seniority": "senior",
    }

    evidence = _local_match_evidence(candidate_fp, job_fp, ["project manager"])

    assert evidence["local_pre_score"] > 30
    assert evidence["role_alignment"] >= 0.7
    assert evidence["required_skill_overlap"] >= 0.45


@pytest.mark.asyncio
async def test_score_compatibility_rejects_obvious_mismatch_without_claude_call():
    backend = FakeMatchCacheBackend()
    client = make_client(
        {
            "score": 99,
            "skills_match": 99,
            "experience_match": 99,
            "domain_match": 99,
            "education_match": 99,
            "top_matching_skills": [],
            "missing_required_skills": [],
            "missing_nice_to_have": [],
            "summary": "unused",
        }
    )

    score = await score_compatibility(
        resume_text="IT Project Manager with strong agile delivery experience.",
        job_title="Clinical Psychologist",
        job_description=(
            "Must have psychology degree, therapy practice experience, clinical assessment skills, "
            "and registration as a psychologist."
        ),
        client=client,
        search_keywords=["project manager"],
        profile=make_profile(),
        cache_backend=backend,
        user_id=1,
        job_id=100,
    )

    assert score <= 25
    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_score_compatibility_calls_claude_once_for_plausible_match_and_uses_cache_afterward():
    backend = FakeMatchCacheBackend()
    client = make_client(
        {
            "score": 72,
            "skills_match": 74,
            "experience_match": 76,
            "domain_match": 42,
            "education_match": 60,
            "top_matching_skills": ["stakeholder management", "project delivery"],
            "missing_required_skills": ["capital delivery"],
            "missing_nice_to_have": ["regulated infrastructure"],
            "summary": "Strong adjacent fit.",
        }
    )

    first = await score_compatibility(
        resume_text="IT Project Manager with strong agile delivery experience.",
        job_title="Senior Project Manager",
        job_description=(
            "Required: stakeholder management, project delivery, budget ownership. "
            "Preferred: capital delivery in regulated infrastructure."
        ),
        client=client,
        search_keywords=["project manager"],
        profile=make_profile(),
        cache_backend=backend,
        user_id=1,
        job_id=101,
    )
    second = await score_compatibility(
        resume_text="IT Project Manager with strong agile delivery experience.",
        job_title="Senior Project Manager",
        job_description=(
            "Required: stakeholder management, project delivery, budget ownership. "
            "Preferred: capital delivery in regulated infrastructure."
        ),
        client=client,
        search_keywords=["project manager"],
        profile=make_profile(),
        cache_backend=backend,
        user_id=1,
        job_id=101,
    )

    assert first == second
    assert first > 70
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_score_compatibility_includes_weak_history_signal_as_small_boost():
    backend = FakeMatchCacheBackend()
    backend.examples = [
        SimpleNamespace(
            id=77,
            title="Senior Project Manager",
            company="Acme Infra",
            description="Owned stakeholder-heavy project delivery and budget management.",
            application_status="applied",
            skills=json.dumps(["stakeholder management", "project delivery", "budget ownership"]),
            notes="Applied successfully to similar delivery role.",
        )
    ]
    client = make_client(
        {
            "score": 66,
            "skills_match": 68,
            "experience_match": 70,
            "domain_match": 35,
            "education_match": 60,
            "top_matching_skills": ["stakeholder management", "project delivery"],
            "missing_required_skills": ["capital delivery"],
            "missing_nice_to_have": [],
            "summary": "Solid adjacent fit.",
        }
    )

    score = await score_compatibility(
        resume_text="IT Project Manager with strong agile delivery experience.",
        job_title="Senior Project Manager",
        job_description=(
            "Required: stakeholder management, project delivery, budget ownership. "
            "Preferred: capital delivery."
        ),
        client=client,
        search_keywords=["project manager"],
        profile=make_profile(),
        cache_backend=backend,
        user_id=1,
        job_id=101,
    )

    assert score > 66
