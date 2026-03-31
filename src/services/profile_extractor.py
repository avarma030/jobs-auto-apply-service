from __future__ import annotations

import json
from typing import TYPE_CHECKING

import anthropic
from loguru import logger

from src.services.ai_contracts import ProfileExtractionResult, ScreeningAnswerBatchResult
from src.services.ai_gateway import call_json_prompt
from src.services.ai_knowledge import (
    build_candidate_knowledge_pack,
    ensure_candidate_knowledge_pack,
    load_resume_text_from_profile,
    resolve_answer_memory,
    select_evidence_snippets,
    stable_hash,
    sync_profile_answer_memory,
)
from src.services.prompt_registry import (
    PROFILE_EXTRACTION_PROMPT,
    SCREENING_ANSWER_PROMPT,
    render_profile_extraction_prompt,
    render_screening_answer_prompt,
)

if TYPE_CHECKING:
    from src.appliers.base import ApplicationQuestionPrompt
    from src.models.user_profile import UserProfile


async def extract_profile_from_resume(
    resume_text: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    *,
    cache_backend=None,
    user_id: int | None = None,
) -> dict:
    """
    Ask Claude to extract structured profile data from raw resume text.
    Returns a dict compatible with UserProfile / profile_json blob.
    """
    if not resume_text.strip():
        return {}

    try:
        result = await call_json_prompt(
            client,
            spec=PROFILE_EXTRACTION_PROMPT,
            prompt=render_profile_extraction_prompt(resume_text),
            model=model,
            max_tokens=2048,
            response_model=ProfileExtractionResult,
            context="profile extraction",
            cache_backend=cache_backend,
            source_hash=stable_hash(
                PROFILE_EXTRACTION_PROMPT.version,
                resume_text[:6000],
            ),
            user_id=user_id,
            metadata={"resume_excerpt_length": min(len(resume_text), 6000)},
        )
        data = result.model_dump(mode="json")
        logger.info(
            f"Extracted profile: {data.get('first_name')} {data.get('last_name')}, "
            f"{len(data.get('skills', []))} skills, "
            f"{len(data.get('work_experience', []))} jobs"
        )
        return data
    except Exception as exc:
        logger.error(f"Profile extraction error: {exc}")
        return {}


async def suggest_answers(
    questions: list[str] | list["ApplicationQuestionPrompt"],
    profile: "UserProfile",
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    *,
    cache_backend=None,
    user_id: int | None = None,
    resume_text: str | None = None,
) -> dict[str, str]:
    """
    Resolve screening questions using answer memory first, then one Claude call
    for any unresolved prompts using shared candidate knowledge and evidence.
    """
    if not questions:
        return {}

    question_entries: list[dict[str, object]] = []
    for item in questions:
        if hasattr(item, "question"):
            question_entries.append(
                {
                    "question": str(getattr(item, "question", "")).strip(),
                    "field_type": str(getattr(item, "field_type", "text")).strip() or "text",
                    "options": [
                        str(option).strip()
                        for option in getattr(item, "options", [])
                        if str(option).strip()
                    ],
                }
            )
        else:
            question_entries.append(
                {
                    "question": str(item).strip(),
                    "field_type": "text",
                    "options": [],
                }
            )

    question_entries = [entry for entry in question_entries if entry["question"]]
    if not question_entries:
        return {}

    resume_text = resume_text if resume_text is not None else load_resume_text_from_profile(profile)

    if cache_backend is not None and user_id is not None:
        await sync_profile_answer_memory(cache_backend, user_id=user_id, profile=profile)

    if cache_backend is not None and user_id is not None and resume_text.strip():
        candidate_pack = await ensure_candidate_knowledge_pack(
            cache_backend,
            user_id=user_id,
            profile=profile,
            resume_text=resume_text,
        )
    else:
        candidate_pack = build_candidate_knowledge_pack(profile, resume_text)

    memory_hits = await resolve_answer_memory(
        cache_backend,
        user_id=user_id,
        prompts=question_entries,
        candidate_pack=candidate_pack,
    )

    resolved: dict[str, str] = {
        question_text: hit.answer_text for question_text, hit in memory_hits.items()
    }
    unresolved = [
        entry
        for entry in question_entries
        if str(entry["question"]) not in resolved
    ]
    if not unresolved:
        return resolved

    memory_hint_payload = [
        {
            "question": question_text,
            "answer": hit.answer_text,
            "source_kind": hit.source_kind,
            "confidence": round(hit.confidence, 3),
            "evidence": hit.evidence,
        }
        for question_text, hit in memory_hits.items()
    ]

    combined_query = " ".join(str(entry["question"]) for entry in unresolved)
    evidence_snippets = select_evidence_snippets(
        candidate_pack.evidence_snippets,
        combined_query,
        limit=6,
    )
    prompt = render_screening_answer_prompt(
        candidate_snapshot=candidate_pack.snapshot,
        candidate_skills=candidate_pack.skills,
        candidate_titles=candidate_pack.titles,
        evidence_snippets=evidence_snippets,
        memory_hints=memory_hint_payload,
        questions=unresolved,
    )

    try:
        result = await call_json_prompt(
            client,
            spec=SCREENING_ANSWER_PROMPT,
            prompt=prompt,
            model=model,
            max_tokens=900,
            response_model=ScreeningAnswerBatchResult,
            context="screening answer suggestions",
            cache_backend=cache_backend,
            source_hash=stable_hash(
                SCREENING_ANSWER_PROMPT.version,
                candidate_pack.source_hash,
                json.dumps(unresolved, ensure_ascii=False, sort_keys=True),
            ),
            user_id=user_id,
            metadata={"question_count": len(unresolved)},
        )
        for item in result.answers:
            question_text = str(item.question).strip()
            answer_text = str(item.answer).strip()
            if question_text and answer_text:
                resolved[question_text] = answer_text
        return resolved
    except Exception as exc:
        logger.error(f"suggest_answers error: {exc}")
        return resolved
