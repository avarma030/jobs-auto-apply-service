from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.appliers.base import ApplicationQuestionPrompt
from src.database import Database
from src.database.models import (
    CandidateKnowledgePackRecord,
    JobKnowledgePackRecord,
    SemanticCacheRecord,
    User,
)
from src.models import Job, UserProfile
from src.models.user_profile import Address, SocialLinks, WorkExperience
from src.services.ai_knowledge import (
    build_candidate_knowledge_pack,
    ensure_candidate_knowledge_pack,
    ensure_job_knowledge_pack,
    resolve_answer_memory,
    store_answer_memory,
)
from src.services.profile_extractor import suggest_answers


async def _make_db(tmp_path: Path) -> tuple[Database, User]:
    db_path = tmp_path / "test.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()
    async with db.session_factory() as session:
        user = User(email="tenant@example.com", hashed_password="hashed")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return db, user


def make_profile() -> UserProfile:
    return UserProfile(
        first_name="Rucha",
        last_name="Varma",
        email="rucha@example.com",
        phone="+353 871234567",
        address=Address(city="Dublin", state="Leinster", country="Ireland"),
        headline="Senior Project Manager | Agile Delivery Lead",
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
                location="Dublin",
            ),
            WorkExperience(
                company="Example Corp",
                title="Project Manager",
                start_date="2018-01",
                end_date="2020-12",
                description="Managed delivery plans, budgets, and project stakeholders.",
                location="Dublin",
            ),
        ],
        social_links=SocialLinks(),
        custom_answers={
            "Are you legally authorized to work in Ireland?": "Yes",
            "Will you require sponsorship now or in the future?": "No",
        },
    )


def make_job() -> Job:
    return Job(
        title="Senior Project Manager",
        company="Acme Infra",
        location="Dublin",
        description=(
            "Required: stakeholder management, project delivery, budget ownership. "
            "Preferred: capital delivery in regulated infrastructure."
        ),
        url="https://example.com/jobs/123",
        source_board="linkedin",
    )


@pytest.mark.asyncio
async def test_candidate_knowledge_pack_is_cached_and_invalidates_on_resume_change(tmp_path):
    db, user = await _make_db(tmp_path)
    try:
        profile = make_profile()
        resume_text = "Project manager with agile delivery, stakeholder management, and scrum."
        pack_one = await ensure_candidate_knowledge_pack(
            db,
            user_id=user.id,
            profile=profile,
            resume_text=resume_text,
        )
        pack_two = await ensure_candidate_knowledge_pack(
            db,
            user_id=user.id,
            profile=profile,
            resume_text=resume_text,
        )
        pack_three = await ensure_candidate_knowledge_pack(
            db,
            user_id=user.id,
            profile=profile,
            resume_text=resume_text + " Added cloud transformation delivery.",
        )

        assert pack_one.source_hash == pack_two.source_hash
        assert pack_three.source_hash != pack_one.source_hash

        async with db.session_factory() as session:
            rows = list((await session.execute(select(CandidateKnowledgePackRecord))).scalars().all())
            assert len(rows) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_job_knowledge_pack_is_cached_per_job_source_hash(tmp_path):
    db, user = await _make_db(tmp_path)
    try:
        job = make_job()
        pack_one = await ensure_job_knowledge_pack(db, job_id=1, user_id=user.id, job=job)
        pack_two = await ensure_job_knowledge_pack(db, job_id=1, user_id=user.id, job=job)

        assert pack_one.source_hash == pack_two.source_hash

        async with db.session_factory() as session:
            rows = list((await session.execute(select(JobKnowledgePackRecord))).scalars().all())
            assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_answer_memory_reuses_only_option_compatible_answers(tmp_path):
    db, user = await _make_db(tmp_path)
    try:
        profile = make_profile()
        candidate_pack = build_candidate_knowledge_pack(profile, "Resume with project delivery and stakeholder management.")
        await store_answer_memory(
            db,
            user_id=user.id,
            question_text="Are you legally authorized to work in Ireland?",
            answer_text="Yes",
            source_kind="profile",
            confidence=0.99,
            approved=True,
            evidence=["Profile says authorized to work in Ireland."],
        )

        allowed = await resolve_answer_memory(
            db,
            user_id=user.id,
            prompts=[
                {
                    "question": "Are you legally authorized to work in Ireland?",
                    "field_type": "radio",
                    "options": ["Yes", "No"],
                }
            ],
            candidate_pack=candidate_pack,
        )
        blocked = await resolve_answer_memory(
            db,
            user_id=user.id,
            prompts=[
                {
                    "question": "Are you legally authorized to work in Ireland?",
                    "field_type": "radio",
                    "options": ["No", "Maybe"],
                }
            ],
            candidate_pack=candidate_pack,
        )

        assert allowed["Are you legally authorized to work in Ireland?"].answer_text == "Yes"
        assert blocked == {}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_suggest_answers_uses_memory_before_calling_claude(tmp_path):
    db, user = await _make_db(tmp_path)
    try:
        profile = make_profile()
        await store_answer_memory(
            db,
            user_id=user.id,
            question_text="Will you require sponsorship now or in the future?",
            answer_text="No",
            source_kind="profile",
            confidence=0.99,
            approved=True,
            evidence=["Profile custom answer says no sponsorship needed."],
        )
        client = AsyncMock()
        client.messages.create = AsyncMock()

        answers = await suggest_answers(
            [
                ApplicationQuestionPrompt(
                    question="Will you require sponsorship now or in the future?",
                    field_type="radio",
                    options=["Yes", "No"],
                )
            ],
            profile,
            client,
            cache_backend=db,
            user_id=user.id,
            resume_text="Project manager with agile delivery and stakeholder management.",
        )

        assert answers == {"Will you require sponsorship now or in the future?": "No"}
        client.messages.create.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_suggest_answers_caches_unresolved_batch_after_first_claude_call(tmp_path):
    db, user = await _make_db(tmp_path)
    try:
        profile = make_profile()
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "answers": [
                                    {
                                        "question": "How many years of experience do you have with Scrum?",
                                        "answer": "8",
                                        "confidence": 0.93,
                                        "evidence": ["8 years of project delivery and scrum leadership."],
                                    }
                                ]
                            }
                        )
                    )
                ]
            )
        )

        prompts = [
            ApplicationQuestionPrompt(
                question="How many years of experience do you have with Scrum?",
                field_type="number",
                options=[],
            )
        ]
        first = await suggest_answers(
            prompts,
            profile,
            client,
            cache_backend=db,
            user_id=user.id,
            resume_text="8 years leading scrum delivery teams and project execution.",
        )
        second = await suggest_answers(
            prompts,
            profile,
            client,
            cache_backend=db,
            user_id=user.id,
            resume_text="8 years leading scrum delivery teams and project execution.",
        )

        assert first == second == {"How many years of experience do you have with Scrum?": "8"}
        assert client.messages.create.await_count == 1

        async with db.session_factory() as session:
            rows = list((await session.execute(select(SemanticCacheRecord))).scalars().all())
            assert any(row.prompt_name == "screening_answers" for row in rows)
    finally:
        await db.close()
