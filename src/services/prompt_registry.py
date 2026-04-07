from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    kind: str
    name: str
    version: str
    system: str


PROFILE_EXTRACTION_PROMPT = PromptSpec(
    kind="profile_extraction",
    name="profile_extraction",
    version="v2",
    system=(
        "You are an expert resume parser. Extract only factual structured candidate data "
        "from the provided resume text and return valid JSON."
    ),
)

MATCH_SCORING_PROMPT = PromptSpec(
    kind="match_score",
    name="match_scoring",
    version="v2",
    system=(
        "You are an industry-standard ATS reviewer, recruiter, and career consultant with "
        "20+ years of hiring experience. Judge fit quickly but accurately, be generous for "
        "transferable adjacent fit, and return only valid JSON."
    ),
)

SCREENING_ANSWER_PROMPT = PromptSpec(
    kind="screening_answers",
    name="screening_answers",
    version="v2",
    system=(
        "You help a job applicant answer application questions honestly and tersely. "
        "Use the supplied evidence and prior memory first, and return only valid JSON."
    ),
)

ATS_SCORE_PROMPT = PromptSpec(
    kind="ats_score",
    name="ats_score",
    version="v2",
    system=(
        "You simulate a modern ATS and score resume-to-job compatibility. "
        "Return only valid JSON."
    ),
)

RESUME_TAILOR_PROMPT = PromptSpec(
    kind="resume_tailor",
    name="resume_tailor",
    version="v3",
    system=(
        "You are an expert resume writer specializing in ATS optimization. "
        "Rewrite only with factual information and return only valid JSON. "
        "Do not output the resume directly outside the JSON object. "
        "Escape newlines inside JSON string values."
    ),
)

COVER_LETTER_PROMPT = PromptSpec(
    kind="cover_letter",
    name="cover_letter",
    version="v2",
    system=(
        "You write concise, professional cover letters grounded strictly in the supplied "
        "candidate and job evidence. Return only valid JSON."
    ),
)


def render_profile_extraction_prompt(resume_text: str) -> str:
    return f"""Extract structured profile information from this resume and return JSON matching:
{{
  "first_name": "<string or null>",
  "last_name": "<string or null>",
  "email": "<string or null>",
  "phone": "<string or null>",
  "headline": "<string or null>",
  "summary": "<string or null>",
  "years_of_experience": <integer or null>,
  "skills": ["skill1", "skill2"],
  "languages": ["English"],
  "work_experience": [
    {{
      "company": "<string>",
      "title": "<string>",
      "start_date": "<YYYY-MM or YYYY>",
      "end_date": "<YYYY-MM or YYYY or null>",
      "description": "<string or null>",
      "location": "<string or null>"
    }}
  ],
  "education": [
    {{
      "institution": "<string>",
      "degree": "<string>",
      "field_of_study": "<string or null>",
      "start_date": "<YYYY or null>",
      "end_date": "<YYYY or null>",
      "gpa": <float or null>
    }}
  ],
  "social_links": {{
    "linkedin": "<url or null>",
    "github": "<url or null>",
    "portfolio": "<url or null>",
    "twitter": "<url or null>"
  }},
  "address": {{
    "city": "<string or null>",
    "state": "<string or null>",
    "country": "<string or null>",
    "zip_code": "<string or null>",
    "street": "<string or null>"
  }}
}}

Rules:
- Extract only facts that are explicitly supported by the resume
- Use null or [] for missing fields
- Return only the JSON object

<resume>
{resume_text[:6000]}
</resume>"""


def render_match_scoring_prompt(
    *,
    search_keywords: list[str] | None,
    candidate_snapshot: str,
    candidate_titles: list[str],
    candidate_skills: list[str],
    candidate_domains: list[str],
    job_title: str,
    job_pack: dict,
    local_evidence: dict,
    history_hints: list[dict],
    resume_excerpt: str,
    job_description_excerpt: str,
) -> str:
    return f"""Evaluate this candidate-job match.

<search_intent>
{", ".join(search_keywords or []) or "Not provided"}
</search_intent>

<candidate_profile_snapshot>
{candidate_snapshot or "Not provided"}
</candidate_profile_snapshot>

<candidate_titles>
{json.dumps(candidate_titles, ensure_ascii=False)}
</candidate_titles>

<candidate_skills>
{json.dumps(candidate_skills[:18], ensure_ascii=False)}
</candidate_skills>

<candidate_domains>
{json.dumps(candidate_domains[:10], ensure_ascii=False)}
</candidate_domains>

<job_title>{job_title}</job_title>

<job_requirements>
{json.dumps(job_pack, ensure_ascii=False)}
</job_requirements>

<local_match_evidence>
{json.dumps(local_evidence, ensure_ascii=False)}
</local_match_evidence>

<history_hints>
{json.dumps(history_hints, ensure_ascii=False)}
</history_hints>

<resume_excerpt>
{resume_excerpt[:3500]}
</resume_excerpt>

<job_description_excerpt>
{job_description_excerpt[:4200]}
</job_description_excerpt>

Respond with JSON:
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


def render_screening_answer_prompt(
    *,
    candidate_snapshot: str,
    candidate_skills: list[str],
    candidate_titles: list[str],
    evidence_snippets: list[str],
    memory_hints: list[dict],
    questions: list[dict],
) -> str:
    return f"""Based on the candidate context below, answer the unresolved screening questions honestly.

<candidate_profile_snapshot>
{candidate_snapshot or "Not provided"}
</candidate_profile_snapshot>

<candidate_titles>
{json.dumps(candidate_titles[:10], ensure_ascii=False)}
</candidate_titles>

<candidate_skills>
{json.dumps(candidate_skills[:18], ensure_ascii=False)}
</candidate_skills>

<evidence_snippets>
{json.dumps(evidence_snippets[:8], ensure_ascii=False)}
</evidence_snippets>

<prior_answer_memory_hints>
{json.dumps(memory_hints[:5], ensure_ascii=False)}
</prior_answer_memory_hints>

<questions>
{json.dumps(questions, ensure_ascii=False)}
</questions>

Rules:
- Keep answers short and form-safe
- For numeric fields, return digits only
- If options are supplied, choose one of the options exactly
- If the evidence is insufficient, answer conservatively
- Return JSON in this exact shape:
{{
  "answers": [
    {{
      "question": "<question text>",
      "answer": "<answer>",
      "confidence": <number 0-1>,
      "evidence": ["short supporting snippet"]
    }}
  ]
}}"""


def render_ats_score_prompt(
    *,
    resume_text: str,
    job_description: str,
    candidate_pack: dict | None,
    job_pack: dict | None,
) -> str:
    return f"""Score this resume against the job description for ATS pass-through likelihood.

<candidate_knowledge>
{json.dumps(candidate_pack or {}, ensure_ascii=False)}
</candidate_knowledge>

<job_knowledge>
{json.dumps(job_pack or {}, ensure_ascii=False)}
</job_knowledge>

<job_description>
{job_description[:3000]}
</job_description>

<resume>
{resume_text[:3000]}
</resume>

Respond with JSON:
{{
  "ats_score": <integer 0-100>,
  "keyword_matches": ["kw1", "kw2"],
  "missing_keywords": ["kw1", "kw2"],
  "recommendation": "<one sentence>"
}}"""


def render_resume_tailor_prompt(
    *,
    resume_text: str,
    job_title: str,
    job_description: str,
    candidate_pack: dict | None,
    job_pack: dict | None,
) -> str:
    return f"""Rewrite the candidate's resume to maximize ATS score for the target role.

Rules:
- Preserve all factual information
- Naturally incorporate exact job keywords where truthful
- Keep formatting clean plain text
- Return only a JSON object with escaped newlines inside string values
- Do not include markdown fences or explanatory text

<candidate_knowledge>
{json.dumps(candidate_pack or {}, ensure_ascii=False)}
</candidate_knowledge>

<job_knowledge>
{json.dumps(job_pack or {}, ensure_ascii=False)}
</job_knowledge>

<job_title>{job_title}</job_title>

<job_description>
{job_description[:4000]}
</job_description>

<original_resume>
{resume_text[:4000]}
</original_resume>

Respond with JSON:
{{
  "tailored_resume_text": "<complete rewritten resume text>",
  "change_summary": ["change 1", "change 2"]
}}"""


def render_cover_letter_prompt(
    *,
    candidate_snapshot: str,
    job_title: str,
    company: str,
    job_description: str,
    candidate_pack: dict | None,
    job_pack: dict | None,
    tailored_resume_text: str,
    candidate_name: str,
) -> str:
    return f"""Write a concise professional cover letter and return only valid JSON.

<candidate_name>{candidate_name}</candidate_name>
<candidate_profile_snapshot>
{candidate_snapshot}
</candidate_profile_snapshot>

<candidate_knowledge>
{json.dumps(candidate_pack or {}, ensure_ascii=False)}
</candidate_knowledge>

<job_knowledge>
{json.dumps(job_pack or {}, ensure_ascii=False)}
</job_knowledge>

<job_title>{job_title}</job_title>
<company>{company}</company>

<job_description>
{job_description[:3000]}
</job_description>

<tailored_resume_highlights>
{tailored_resume_text[:2200]}
</tailored_resume_highlights>

Requirements:
- 3 paragraphs only, about 250-300 words total
- Start with Dear Hiring Team,
- End with Sincerely, then the candidate name

Respond with JSON:
{{
  "cover_letter": "<full letter body>"
}}"""
