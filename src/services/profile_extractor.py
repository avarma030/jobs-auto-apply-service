from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import anthropic
from loguru import logger

if TYPE_CHECKING:
    from src.models.user_profile import UserProfile


async def extract_profile_from_resume(
    resume_text: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Ask Claude to extract structured profile data from raw resume text.
    Returns a dict compatible with UserProfile / profile_json blob.

    Fields extracted:
      first_name, last_name, email, phone, headline, summary,
      years_of_experience, skills, languages,
      work_experience, education, social_links
    """
    prompt = f"""You are an expert resume parser. Extract all structured profile information
from the resume text below and return it as valid JSON.

<resume>
{resume_text[:6000]}
</resume>

Return ONLY valid JSON matching this exact structure (use null for missing fields,
empty list [] for missing arrays):
{{
  "first_name": "<string or null>",
  "last_name": "<string or null>",
  "email": "<string or null>",
  "phone": "<string or null>",
  "headline": "<one-line professional title, e.g. 'Senior Software Engineer'>",
  "summary": "<2-4 sentence professional summary>",
  "years_of_experience": <integer or null>,
  "skills": ["skill1", "skill2"],
  "languages": ["English", "Spanish"],
  "work_experience": [
    {{
      "company": "<string>",
      "title": "<string>",
      "start_date": "<YYYY-MM or YYYY>",
      "end_date": "<YYYY-MM or YYYY or null if current>",
      "description": "<brief bullet-point summary>",
      "location": "<string or null>"
    }}
  ],
  "education": [
    {{
      "institution": "<string>",
      "degree": "<e.g. Bachelor of Science>",
      "field_of_study": "<e.g. Computer Science>",
      "start_date": "<YYYY or null>",
      "end_date": "<YYYY or null>",
      "gpa": <float or null>
    }}
  ],
  "social_links": {{
    "linkedin": "<URL or null>",
    "github": "<URL or null>",
    "portfolio": "<URL or null>",
    "twitter": null
  }},
  "address": {{
    "city": "<string or null>",
    "state": "<string or null>",
    "country": "<string or null>",
    "zip_code": null,
    "street": null
  }}
}}

Rules:
- Extract only what is actually in the resume — do not fabricate
- years_of_experience: calculate from earliest work start date to present
- skills: include both hard skills (Python, React) and domain skills (Machine Learning)
- For current jobs, set end_date to null
- Return ONLY the JSON object, no commentary"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning(f"No JSON in profile extraction response: {raw[:300]}")
            return {}
        data = json.loads(json_match.group())
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
    questions: list[str],
    profile: "UserProfile",
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> dict[str, str]:
    """
    Given a list of unanswered job application screening questions, use Claude
    to generate short, appropriate answers based on the candidate's profile.

    Returns dict[question_text -> answer_string].
    Answers are intentionally brief (a word, number, or short phrase) to fit
    form text inputs, radio buttons, and select dropdowns.
    """
    if not questions:
        return {}

    # Build a compact profile context
    name = f"{profile.first_name or ''} {profile.last_name or ''}".strip()
    skills_str = ", ".join(profile.skills[:20]) if profile.skills else "not specified"
    country = ""
    if profile.address:
        country = profile.address.country or ""
    yoe = profile.years_of_experience or "unknown"

    questions_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))

    prompt = f"""You are helping a job applicant fill out screening questions on a job application form.
Based on the candidate's profile, provide short, honest answers to each question.

Candidate profile:
- Name: {name or 'not provided'}
- Years of experience: {yoe}
- Skills: {skills_str}
- Country: {country or 'not specified'}

Screening questions:
{questions_block}

Rules:
- Answers must be SHORT — a single word, number, Yes/No, or a brief phrase (≤ 10 words)
- These answers go directly into form fields or are selected from dropdowns
- Be honest based on the profile — do not fabricate
- For yes/no questions use "Yes" or "No" exactly
- For numeric questions (years of experience) use a plain integer
- For authorization/sponsorship questions: if country is US or not specified, answer "Yes" for authorization, "No" for sponsorship needed

Return ONLY valid JSON:
{{
  "<question 1 text>": "<answer>",
  "<question 2 text>": "<answer>"
}}"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning(f"No JSON in suggest_answers response: {raw[:200]}")
            return {}
        data = json.loads(json_match.group())
        # Ensure all values are strings
        return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        logger.error(f"suggest_answers error: {exc}")
        return {}
