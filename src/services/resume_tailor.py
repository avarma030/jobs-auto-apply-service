from __future__ import annotations

from loguru import logger

import anthropic

from src.services.ai_contracts import ATSScoreResult, TailoredResumeResult
from src.services.ai_gateway import call_json_prompt
from src.services.ai_knowledge import stable_hash
from src.services.prompt_registry import (
    ATS_SCORE_PROMPT,
    RESUME_TAILOR_PROMPT,
    render_ats_score_prompt,
    render_resume_tailor_prompt,
)


async def tailor_resume(
    resume_text: str,
    job_title: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    target_ats_score: float = 90.0,
    max_attempts: int = 3,
    *,
    candidate_pack: dict | None = None,
    job_pack: dict | None = None,
    cache_backend=None,
    user_id: int | None = None,
) -> tuple[str, float]:
    """
    Iteratively tailor a resume until ATS score >= target_ats_score.
    Returns (tailored_resume_text, final_ats_score).
    """
    current_resume = resume_text
    current_score = 0.0

    for attempt in range(1, max_attempts + 1):
        logger.info(f"Tailoring resume for '{job_title}' - attempt {attempt}/{max_attempts}")
        result = await _tailor_once(
            current_resume,
            job_title,
            job_description,
            client,
            model,
            candidate_pack=candidate_pack,
            job_pack=job_pack,
            cache_backend=cache_backend,
            user_id=user_id,
        )
        if result.strip():
            current_resume = result
        else:
            logger.warning(f"Tailoring attempt {attempt} returned empty - keeping previous version")
        current_score = await score_ats(
            current_resume,
            job_description,
            client,
            model,
            candidate_pack=candidate_pack,
            job_pack=job_pack,
            cache_backend=cache_backend,
            user_id=user_id,
        )
        logger.info(f"ATS score after attempt {attempt}: {current_score:.0f}%")

        if current_score >= target_ats_score:
            break

    return current_resume, current_score


async def _tailor_once(
    resume_text: str,
    job_title: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str,
    *,
    candidate_pack: dict | None = None,
    job_pack: dict | None = None,
    cache_backend=None,
    user_id: int | None = None,
) -> str:
    result = await call_json_prompt(
        client,
        spec=RESUME_TAILOR_PROMPT,
        prompt=render_resume_tailor_prompt(
            resume_text=resume_text,
            job_title=job_title,
            job_description=job_description,
            candidate_pack=candidate_pack,
            job_pack=job_pack,
        ),
        model=model,
        max_tokens=2600,
        response_model=TailoredResumeResult,
        context=f"resume tailoring for {job_title}",
        cache_backend=cache_backend,
        source_hash=stable_hash(
            RESUME_TAILOR_PROMPT.version,
            job_title,
            job_description[:4000],
            resume_text[:4000],
        ),
        user_id=user_id,
        metadata={"job_title": job_title},
        text_fallback_field="tailored_resume_text",
        text_fallback_min_length=80,
    )
    return result.tailored_resume_text.strip()


async def score_ats(
    resume_text: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    *,
    candidate_pack: dict | None = None,
    job_pack: dict | None = None,
    cache_backend=None,
    user_id: int | None = None,
) -> float:
    """Ask Claude to rate ATS compatibility of resume vs job description (0-100)."""
    if not (job_description or "").strip():
        logger.warning("ATS scoring skipped - job description is empty")
        return 0.0

    try:
        result = await call_json_prompt(
            client,
            spec=ATS_SCORE_PROMPT,
            prompt=render_ats_score_prompt(
                resume_text=resume_text,
                job_description=job_description,
                candidate_pack=candidate_pack,
                job_pack=job_pack,
            ),
            model=model,
            max_tokens=1024,
            response_model=ATSScoreResult,
            context="ats score",
            cache_backend=cache_backend,
            source_hash=stable_hash(
                ATS_SCORE_PROMPT.version,
                resume_text[:3000],
                job_description[:3000],
            ),
            user_id=user_id,
        )
        return min(max(float(result.ats_score), 0.0), 100.0)
    except Exception as exc:
        logger.error(f"ATS scoring error: {exc}")
        return 0.0
