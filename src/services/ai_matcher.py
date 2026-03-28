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

    # Use up to 6000 chars each — richer context improves accuracy
    jd_text = job_description[:6000]
    cv_text = resume_text[:6000]

    prompt = f"""You are a senior technical recruiter with 15 years of experience placing candidates
at top tech companies. Your job is to evaluate how well a candidate's background matches a job
posting — the same way a human hiring manager would, NOT as a keyword-matching ATS.

SCORING PHILOSOPHY:
- Be realistic and generous when the candidate's background genuinely aligns with the role
- A candidate who has directly done this type of work before should score 80-95%
- A strong adjacent background with transferable skills should score 65-80%
- Someone learning or pivoting into this role should score 40-65%
- A clearly mismatched profile (e.g. marketing manager applying for data engineer) should score <40%
- DO NOT compress scores toward 50% — use the full range confidently

CALIBRATION EXAMPLES:
- "Data scientist with 3 years of ML/Python applying for Senior Data Scientist" → ~88%
- "Software engineer with 5yr Python/Django applying for Backend Engineer (Python)" → ~90%
- "Data analyst (SQL, Tableau) applying for Data Scientist (Python, ML required)" → ~62%
- "Frontend React dev applying for Full Stack (React + Node)" → ~78%
- "Marketing manager applying for Data Engineer" → ~18%

IMPORTANT: The job title is a strong signal. If the candidate's most recent role title closely
matches the job title, weight that heavily in your score.

<job_title>{job_title}</job_title>

<job_description>
{jd_text}
</job_description>

<candidate_resume>
{cv_text}
</candidate_resume>

Analyze the match across these weighted dimensions:
1. Core skills & technologies — does the candidate have the must-have tools/languages/frameworks? (40%)
2. Role-level & experience depth — right seniority, years, scope of work? (25%)
3. Domain/industry fit — right problem space, data types, business context? (20%)
4. Education & certifications — relevant degrees, certs, or equivalent? (15%)

For dimension 1, distinguish "required" vs "nice-to-have" skills in the JD and weight required skills more.

Respond with ONLY valid JSON (no markdown fences):
{{
  "score": <integer 0-100>,
  "skills_match": <integer 0-100>,
  "experience_match": <integer 0-100>,
  "domain_match": <integer 0-100>,
  "education_match": <integer 0-100>,
  "top_matching_skills": ["skill1", "skill2", "skill3"],
  "missing_required_skills": ["skill1", "skill2"],
  "missing_nice_to_have": ["skill1"],
  "summary": "<2-3 sentence honest assessment of fit>"
}}"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=768,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning(f"No JSON in match response for '{job_title}': {raw[:200]}")
            return 0.0
        data = json.loads(json_match.group())
        score = float(data.get("score", 0))
        missing = data.get("missing_required_skills") or data.get("missing_skills") or []
        logger.info(
            f"Match score for '{job_title}': {score:.0f}% "
            f"(skills={data.get('skills_match')}%, "
            f"exp={data.get('experience_match')}%, "
            f"domain={data.get('domain_match')}%) | "
            f"missing: {missing[:3]}"
        )
        return min(max(score, 0.0), 100.0)
    except Exception as exc:
        logger.error(f"Error scoring '{job_title}': {exc}")
        return 0.0
