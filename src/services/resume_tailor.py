from __future__ import annotations

import json
import re

import anthropic
from loguru import logger


async def tailor_resume(
    resume_text: str,
    job_title: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    target_ats_score: float = 90.0,
    max_attempts: int = 3,
) -> tuple[str, float]:
    """
    Iteratively tailor a resume until ATS score >= target_ats_score.
    Returns (tailored_resume_text, final_ats_score).
    """
    current_resume = resume_text
    current_score = 0.0

    for attempt in range(1, max_attempts + 1):
        logger.info(f"Tailoring resume for '{job_title}' — attempt {attempt}/{max_attempts}")
        current_resume = await _tailor_once(
            current_resume, job_title, job_description, client, model
        )
        current_score = await score_ats(current_resume, job_description, client, model)
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
) -> str:
    prompt = f"""You are an expert resume writer specialising in ATS optimisation.
Rewrite the candidate's resume to maximise its ATS score for the target job posting.

Rules:
- Preserve ALL factual information (dates, companies, titles, degrees) — never fabricate
- Naturally incorporate exact keywords and phrases from the job description
- Use action verbs and quantify achievements where possible
- Match the required skills section to the job's must-have requirements
- Use standard section headings: SUMMARY, EXPERIENCE, EDUCATION, SKILLS, CERTIFICATIONS
- Keep formatting clean: plain text, no tables, no graphics

<job_title>{job_title}</job_title>

<job_description>
{job_description[:4000]}
</job_description>

<original_resume>
{resume_text[:4000]}
</original_resume>

Return ONLY the complete rewritten resume text. No commentary, no preamble."""

    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def score_ats(
    resume_text: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> float:
    """Ask Claude to rate ATS compatibility of resume vs job description (0-100)."""
    if not (job_description or "").strip():
        logger.warning("ATS scoring skipped — job description is empty")
        return 0.0

    prompt = f"""You are an ATS (Applicant Tracking System) simulator. Score this resume
against the job description for ATS pass-through likelihood.

Scoring criteria:
- Keyword density and exact phrase matches (40%)
- Skills alignment (30%)
- Experience relevance (20%)
- Formatting/parsability (10%)

<job_description>
{job_description[:3000]}
</job_description>

<resume>
{resume_text[:3000]}
</resume>

Respond with ONLY valid JSON (no markdown, no code blocks, no commentary):
{{
  "ats_score": <integer 0-100>,
  "keyword_matches": ["kw1", "kw2"],
  "missing_keywords": ["kw1", "kw2"],
  "recommendation": "<one sentence>"
}}"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning(f"ATS scoring: no JSON found in Claude response — raw: {raw[:300]!r}")
            return 0.0
        data = json.loads(json_match.group())
        score = min(max(float(data.get("ats_score", 0)), 0.0), 100.0)
        return score
    except Exception as exc:
        logger.error(f"ATS scoring error: {exc}")
        return 0.0
