from __future__ import annotations

import anthropic
from loguru import logger

from src.models.job import Job
from src.models.user_profile import UserProfile


async def generate_cover_letter(
    profile: UserProfile,
    job: Job,
    tailored_resume_text: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> str:
    """
    Generate a tailored 3-paragraph cover letter.
    Paragraph 1: enthusiasm for role + company, brief match summary
    Paragraph 2: 2-3 specific achievements from tailored resume that match JD
    Paragraph 3: call to action + sign-off
    """
    name = f"{profile.first_name or ''} {profile.last_name or ''}".strip() or "The Candidate"
    headline = profile.headline or "Software Engineer"
    years = profile.years_of_experience or ""

    prompt = f"""Write a professional, concise cover letter for this job application.

Candidate: {name}
Title: {headline}
{f'Experience: {years} years' if years else ''}

<job_title>{job.title}</job_title>
<company>{job.company}</company>

<job_description>
{(job.description or '')[:3000]}
</job_description>

<tailored_resume_highlights>
{tailored_resume_text[:2000]}
</tailored_resume_highlights>

Requirements:
- 3 paragraphs only, ~250–300 words total
- Paragraph 1: Express genuine enthusiasm for the role and company; briefly state why you're a strong match
- Paragraph 2: Highlight 2-3 specific, quantified achievements from the resume that directly address the job requirements
- Paragraph 3: Restate fit, invite interview, professional close
- Tone: confident but not arrogant, professional, human
- Do NOT include a date header or address block — just the letter body starting with "Dear Hiring Team,"
- End with "Sincerely," followed by the candidate's name on the next line

Return ONLY the cover letter text."""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        logger.error(f"Cover letter generation error: {exc}")
        # Return a basic fallback
        return (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {job.title} position at {job.company}. "
            f"My background as {headline} aligns well with your requirements.\n\n"
            f"I look forward to discussing how I can contribute to your team.\n\n"
            f"Sincerely,\n{name}"
        )
