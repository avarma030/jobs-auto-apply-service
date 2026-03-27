from __future__ import annotations

import json
import re

import anthropic
from loguru import logger


async def score_compatibility(
    resume_text: str,
    job_title: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> float:
    """
    Ask Claude to rate how well the resume matches the job description.
    Returns a float 0–100.
    """
    if not job_description or not job_description.strip():
        logger.warning(f"No job description for '{job_title}' — defaulting score to 0")
        return 0.0

    prompt = f"""You are an expert recruiter and ATS specialist. Evaluate how well this candidate's
resume matches the job posting and return a compatibility score.

<job_title>{job_title}</job_title>

<job_description>
{job_description[:4000]}
</job_description>

<resume>
{resume_text[:4000]}
</resume>

Evaluate the match across these dimensions:
1. Required skills and technologies (40%)
2. Years of experience and seniority level (25%)
3. Domain/industry relevance (20%)
4. Education and certifications (15%)

Respond with ONLY valid JSON in this exact format:
{{
  "score": <integer 0-100>,
  "skills_match": <integer 0-100>,
  "experience_match": <integer 0-100>,
  "domain_match": <integer 0-100>,
  "top_matching_skills": ["skill1", "skill2", "skill3"],
  "missing_skills": ["skill1", "skill2"],
  "summary": "<one sentence explanation>"
}}"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Extract JSON even if wrapped in markdown fences
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning(f"No JSON in match response for '{job_title}': {raw[:200]}")
            return 0.0
        data = json.loads(json_match.group())
        score = float(data.get("score", 0))
        logger.info(
            f"Match score for '{job_title}': {score:.0f}% "
            f"(skills={data.get('skills_match')}%, "
            f"exp={data.get('experience_match')}%)"
        )
        return min(max(score, 0.0), 100.0)
    except Exception as exc:
        logger.error(f"Error scoring '{job_title}': {exc}")
        return 0.0
