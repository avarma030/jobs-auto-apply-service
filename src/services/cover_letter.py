from __future__ import annotations

import anthropic
from loguru import logger

from src.models.job import Job
from src.models.user_profile import UserProfile
from src.services.ai_contracts import CoverLetterResult
from src.services.ai_gateway import call_json_prompt
from src.services.ai_knowledge import stable_hash
from src.services.prompt_registry import (
    COVER_LETTER_PROMPT,
    render_cover_letter_prompt,
)


async def generate_cover_letter(
    profile: UserProfile,
    job: Job,
    tailored_resume_text: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    *,
    candidate_pack: dict | None = None,
    job_pack: dict | None = None,
    cache_backend=None,
    user_id: int | None = None,
) -> str:
    """
    Generate a tailored 3-paragraph cover letter.
    """
    name = f"{profile.first_name or ''} {profile.last_name or ''}".strip() or "The Candidate"
    snapshot = candidate_pack.get("snapshot") if isinstance(candidate_pack, dict) else ""

    try:
        result = await call_json_prompt(
            client,
            spec=COVER_LETTER_PROMPT,
            prompt=render_cover_letter_prompt(
                candidate_snapshot=snapshot or (profile.summary or profile.headline or ""),
                job_title=job.title,
                company=job.company,
                job_description=job.description or "",
                candidate_pack=candidate_pack,
                job_pack=job_pack,
                tailored_resume_text=tailored_resume_text,
                candidate_name=name,
            ),
            model=model,
            max_tokens=1200,
            response_model=CoverLetterResult,
            context=f"cover letter for {job.title}",
            cache_backend=cache_backend,
            source_hash=stable_hash(
                COVER_LETTER_PROMPT.version,
                name,
                job.title,
                job.company,
                (job.description or "")[:3000],
                tailored_resume_text[:2200],
            ),
            user_id=user_id,
            metadata={"job_title": job.title, "company": job.company},
        )
        return result.cover_letter.strip()
    except Exception as exc:
        logger.error(f"Cover letter generation error: {exc}")
        return (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {job.title} position at {job.company}. "
            f"My background aligns well with your requirements.\n\n"
            f"I look forward to discussing how I can contribute to your team.\n\n"
            f"Sincerely,\n{name}"
        )
