from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol

import anthropic
from loguru import logger

if TYPE_CHECKING:
    from src.models import UserProfile


MATCHER_VERSION = "hybrid-cost-optimized-v1"
_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_LOW_SIGNAL_TOKENS = {
    "analyst",
    "architect",
    "associate",
    "consultant",
    "coordinator",
    "developer",
    "engineer",
    "lead",
    "manager",
    "principal",
    "senior",
    "specialist",
    "staff",
}
_RELEVANT_ANSWER_HINTS = (
    "experience",
    "skill",
    "technology",
    "tool",
    "framework",
    "language",
    "certification",
    "certificate",
    "qualification",
    "license",
    "industry",
    "domain",
    "background",
    "title",
    "role",
)
_REQUIRED_MARKERS = (
    "must have",
    "required",
    "requirements",
    "experience with",
    "experience in",
    "strong experience in",
    "hands on experience with",
    "hands-on experience with",
    "proficiency in",
    "expertise in",
    "knowledge of",
    "ability to",
    "responsible for",
)
_PREFERRED_MARKERS = (
    "nice to have",
    "preferred",
    "bonus",
    "plus",
    "desirable",
    "ideally",
    "good to have",
)
_HARD_BLOCKER_MARKERS = (
    "must be",
    "must hold",
    "must possess",
    "must have",
    "required to",
    "mandatory",
    "license required",
    "certification required",
    "eligible to work",
    "right to work",
    "visa",
    "security clearance",
)
_DOMAIN_MARKERS = (
    "industry",
    "sector",
    "environment",
    "domain",
    "context",
    "space",
    "within",
    "across",
)
_SENIORITY_ORDER = {
    "entry": 1,
    "junior": 1,
    "associate": 2,
    "mid": 2,
    "senior": 3,
    "lead": 4,
    "principal": 5,
    "director": 6,
    "executive": 7,
}
_SENIORITY_MARKERS = (
    ("entry", ("entry", "graduate", "junior")),
    ("associate", ("associate", "mid level", "mid-level")),
    ("senior", ("senior", "sr ")),
    ("lead", ("lead", "staff", "head")),
    ("principal", ("principal",)),
    ("director", ("director", "vp", "vice president")),
    ("executive", ("chief", "cxo", "executive")),
)


class MatchCacheBackend(Protocol):
    async def get_semantic_cache(self, key: str, source_hash: str | None = None) -> dict | None:
        ...

    async def upsert_semantic_cache(
        self,
        key: str,
        *,
        kind: str,
        source_hash: str,
        payload: dict,
        user_id: int | None = None,
    ) -> None:
        ...

    async def get_recent_match_examples(
        self,
        user_id: int,
        *,
        limit: int = 12,
        exclude_job_id: int | None = None,
    ) -> list[Any]:
        ...


def _normalize_text(text: str | None) -> str:
    return re.sub(r"[^a-z0-9+#/.-]+", " ", (text or "").lower()).strip()


def _stable_hash(*parts: str) -> str:
    digest = sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_json_loads(raw: str, *, context: str) -> dict[str, Any]:
    raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(raw_clean)
    except (json.JSONDecodeError, ValueError):
        json_match = re.search(r"\{.*\}", raw_clean, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON returned for {context}")
        return json.loads(json_match.group())


def _tokenize(text: str | None) -> list[str]:
    return [
        token
        for token in _normalize_text(text).split()
        if token and token not in _STOPWORDS
    ]


def _token_weight(token: str) -> float:
    return 0.25 if token in _LOW_SIGNAL_TOKENS else 1.0


def _weighted_jaccard(left_tokens: list[str], right_tokens: list[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    left_weights = {token: _token_weight(token) for token in left_tokens}
    right_weights = {token: _token_weight(token) for token in right_tokens}
    union = set(left_weights) | set(right_weights)
    if not union:
        return 0.0
    intersection_score = sum(
        min(left_weights.get(token, 0.0), right_weights.get(token, 0.0))
        for token in union
    )
    union_score = sum(
        max(left_weights.get(token, 0.0), right_weights.get(token, 0.0))
        for token in union
    )
    if not union_score:
        return 0.0
    return intersection_score / union_score


def _phrase_similarity(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = _tokenize(left_norm)
    right_tokens = _tokenize(right_norm)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = _weighted_jaccard(left_tokens, right_tokens)
    left_distinctive = [token for token in left_tokens if token not in _LOW_SIGNAL_TOKENS]
    right_distinctive = [token for token in right_tokens if token not in _LOW_SIGNAL_TOKENS]
    if left_distinctive and set(left_distinctive).issubset(set(right_tokens)):
        overlap = max(overlap, 0.78)
    if right_distinctive and set(right_distinctive).issubset(set(left_tokens)):
        overlap = max(overlap, 0.78)
    if left_norm in right_norm or right_norm in left_norm:
        return max(overlap, 0.78)
    return overlap


def _best_overlap(required_items: list[str], candidate_items: list[str]) -> float:
    if not required_items or not candidate_items:
        return 0.0
    scores: list[float] = []
    for item in required_items:
        scores.append(max(_phrase_similarity(item, candidate) for candidate in candidate_items))
    return sum(scores) / len(scores)


def _dedupe_phrases(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    phrases: list[str] = []
    for value in values:
        normalized = _normalize_text(value)
        if not normalized or normalized in seen:
            continue
        if len(_tokenize(normalized)) == 0:
            continue
        seen.add(normalized)
        phrases.append(normalized)
        if len(phrases) >= limit:
            break
    return phrases


def _split_fragments(text: str) -> list[str]:
    if not text:
        return []
    fragments: list[str] = []
    for line in text.splitlines():
        clean_line = line.strip(" \t-•*")
        if not clean_line:
            continue
        fragments.append(clean_line)
        for segment in re.split(r"[.;]", clean_line):
            segment = segment.strip(" \t-•*")
            if segment and segment != clean_line:
                fragments.append(segment)
    return fragments


def _extract_marked_phrases(text: str, markers: tuple[str, ...], *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in _split_fragments(text):
        lowered = fragment.lower()
        for marker in markers:
            idx = lowered.find(marker)
            if idx == -1:
                continue
            tail = fragment[idx + len(marker):].lstrip(" :-")
            if not tail:
                continue
            for part in re.split(r"[,/;|]| and | or ", tail):
                clean_part = part.strip(" \t-•*")
                token_count = len(_tokenize(clean_part))
                if 2 <= token_count <= 8:
                    phrases.append(clean_part)
            if phrases:
                break
    return _dedupe_phrases(phrases, limit=limit)


def _extract_list_phrases(text: str, *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in _split_fragments(text):
        if len(fragment) > 90:
            continue
        for part in re.split(r"[,/;|]", fragment):
            clean_part = part.strip(" \t-•*")
            token_count = len(_tokenize(clean_part))
            if 2 <= token_count <= 7:
                phrases.append(clean_part)
    return _dedupe_phrases(phrases, limit=limit)


def _extract_domain_phrases(text: str, *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in _split_fragments(text):
        lowered = fragment.lower()
        if any(marker in lowered for marker in _DOMAIN_MARKERS):
            for part in re.split(r"[,/;|]| and | or ", fragment):
                clean_part = part.strip(" \t-•*")
                token_count = len(_tokenize(clean_part))
                if 2 <= token_count <= 6:
                    phrases.append(clean_part)
    return _dedupe_phrases(phrases, limit=limit)


def _extract_seniority(text: str) -> str | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    for seniority, markers in _SENIORITY_MARKERS:
        if any(marker in normalized for marker in markers):
            return seniority
    return None


def _candidate_profile_snapshot(profile: UserProfile | None) -> str:
    if profile is None:
        return ""
    recent_roles = [exp.title for exp in profile.work_experience[:5] if exp.title]
    skills = [skill for skill in profile.skills[:20] if skill]
    relevant_answers = []
    for question, answer in profile.custom_answers.items():
        normalized_question = _normalize_text(question)
        if any(hint in normalized_question for hint in _RELEVANT_ANSWER_HINTS) and answer.strip():
            relevant_answers.append(f"{question}: {answer}")
            if len(relevant_answers) >= 8:
                break
    parts = [
        f"Headline: {profile.headline}" if profile.headline else "",
        f"Summary: {profile.summary}" if profile.summary else "",
        (
            f"Years of experience: {profile.years_of_experience}"
            if profile.years_of_experience is not None
            else ""
        ),
        f"Recent roles: {', '.join(recent_roles)}" if recent_roles else "",
        f"Skills: {', '.join(skills)}" if skills else "",
        f"Relevant Q&A: {' | '.join(relevant_answers)}" if relevant_answers else "",
    ]
    return "\n".join(part for part in parts if part)


def _candidate_titles(profile: UserProfile | None) -> list[str]:
    if profile is None:
        return []
    titles: list[str] = []
    if profile.headline:
        titles.extend([part.strip() for part in re.split(r"[|,/]", profile.headline) if part.strip()])
    titles.extend(exp.title for exp in profile.work_experience[:6] if exp.title)
    return _dedupe_phrases(titles, limit=12)


def _candidate_skills(profile: UserProfile | None, resume_text: str) -> list[str]:
    skills: list[str] = []
    if profile is not None:
        skills.extend(profile.skills[:30])
        for question, answer in profile.custom_answers.items():
            normalized_question = _normalize_text(question)
            if any(hint in normalized_question for hint in _RELEVANT_ANSWER_HINTS):
                skills.extend(re.split(r"[,/;|]", answer))
    skills.extend(_extract_marked_phrases(resume_text[:2500], _REQUIRED_MARKERS, limit=10))
    skills.extend(_extract_list_phrases(resume_text[:1800], limit=12))
    return _dedupe_phrases(skills, limit=32)


def _candidate_domains(profile: UserProfile | None, resume_text: str) -> list[str]:
    text_chunks: list[str] = [resume_text[:1800]]
    if profile is not None:
        if profile.summary:
            text_chunks.append(profile.summary)
        for exp in profile.work_experience[:5]:
            if exp.description:
                text_chunks.append(exp.description)
    domains: list[str] = []
    for chunk in text_chunks:
        domains.extend(_extract_domain_phrases(chunk, limit=6))
        domains.extend(_extract_list_phrases(chunk, limit=6))
    return _dedupe_phrases(domains, limit=16)


def _build_candidate_fingerprint(resume_text: str, profile: UserProfile | None) -> dict[str, Any]:
    titles = _candidate_titles(profile)
    skills = _candidate_skills(profile, resume_text)
    domains = _candidate_domains(profile, resume_text)
    seniority = _extract_seniority(" ".join(titles))
    return {
        "titles": titles,
        "skills": skills,
        "domains": domains,
        "seniority": seniority,
        "snapshot": _candidate_profile_snapshot(profile),
    }


def _build_job_fingerprint(job_title: str, job_description: str) -> dict[str, Any]:
    required_skills = _extract_marked_phrases(job_description, _REQUIRED_MARKERS, limit=16)
    preferred_skills = _extract_marked_phrases(job_description, _PREFERRED_MARKERS, limit=12)
    if not required_skills:
        required_skills = _extract_list_phrases(job_description[:2400], limit=12)
    domains = _extract_domain_phrases(f"{job_title}\n{job_description[:2400]}", limit=10)
    hard_blockers = _extract_marked_phrases(job_description, _HARD_BLOCKER_MARKERS, limit=8)
    title_variants = _dedupe_phrases([job_title] + re.split(r"[-|,/]", job_title), limit=8)
    seniority = _extract_seniority(f"{job_title} {job_description[:400]}")
    return {
        "titles": title_variants,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "domains": domains,
        "hard_blockers": hard_blockers,
        "seniority": seniority,
    }


def _seniority_alignment(candidate_seniority: str | None, job_seniority: str | None) -> float:
    if not candidate_seniority or not job_seniority:
        return 0.5
    left = _SENIORITY_ORDER.get(candidate_seniority, 0)
    right = _SENIORITY_ORDER.get(job_seniority, 0)
    if not left or not right:
        return 0.5
    if left >= right:
        return 1.0
    diff = right - left
    return max(0.0, 1.0 - diff * 0.3)


def _history_similarity(job_fp: dict[str, Any], prior_job: Any) -> float:
    title_similarity = max(
        (_phrase_similarity(title, getattr(prior_job, "title", "") or "") for title in job_fp["titles"]),
        default=0.0,
    )
    prior_skills: list[str] = []
    raw_skills = getattr(prior_job, "skills", None)
    if raw_skills:
        try:
            parsed = json.loads(raw_skills)
            if isinstance(parsed, list):
                prior_skills.extend(str(skill) for skill in parsed)
        except json.JSONDecodeError:
            pass
    prior_skills.extend(_extract_list_phrases(getattr(prior_job, "notes", "") or "", limit=6))
    prior_skills.extend(_extract_list_phrases(getattr(prior_job, "description", "") or "", limit=8))
    skill_similarity = _best_overlap(
        job_fp["required_skills"] or job_fp["preferred_skills"] or job_fp["titles"],
        prior_skills + [getattr(prior_job, "title", "") or ""],
    )
    domain_similarity = _best_overlap(
        job_fp["domains"],
        _extract_domain_phrases(getattr(prior_job, "description", "") or "", limit=6)
        + _extract_list_phrases(getattr(prior_job, "notes", "") or "", limit=4),
    )
    status = str(getattr(prior_job, "application_status", "") or "").lower()
    outcome_weight = {"offered": 1.0, "interviewed": 0.9, "applied": 0.75}.get(status, 0.6)
    return (title_similarity * 0.55 + skill_similarity * 0.35 + domain_similarity * 0.10) * outcome_weight


async def _get_history_hints(
    *,
    cache_backend: MatchCacheBackend | None,
    user_id: int | None,
    job_id: int | None,
    job_fp: dict[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    if cache_backend is None or user_id is None:
        return [], 0.0
    prior_jobs = await cache_backend.get_recent_match_examples(
        user_id,
        limit=12,
        exclude_job_id=job_id,
    )
    hints: list[dict[str, Any]] = []
    best_similarity = 0.0
    for prior_job in prior_jobs:
        similarity = _history_similarity(job_fp, prior_job)
        if similarity < 0.22:
            continue
        best_similarity = max(best_similarity, similarity)
        hints.append(
            {
                "title": getattr(prior_job, "title", "") or "",
                "company": getattr(prior_job, "company", "") or "",
                "status": str(getattr(prior_job, "application_status", "") or ""),
                "similarity": round(similarity, 3),
            }
        )
    hints.sort(key=lambda item: item["similarity"], reverse=True)
    return hints[:3], best_similarity


def _local_match_evidence(
    candidate_fp: dict[str, Any],
    job_fp: dict[str, Any],
    search_keywords: list[str] | None,
) -> dict[str, Any]:
    role_alignment = max(
        (
            _phrase_similarity(job_title, candidate_title)
            for job_title in job_fp["titles"]
            for candidate_title in candidate_fp["titles"]
        ),
        default=0.0,
    )
    required_skill_overlap = _best_overlap(job_fp["required_skills"], candidate_fp["skills"])
    preferred_skill_overlap = _best_overlap(job_fp["preferred_skills"], candidate_fp["skills"])
    seniority_alignment = _seniority_alignment(candidate_fp["seniority"], job_fp["seniority"])
    domain_alignment = _best_overlap(job_fp["domains"], candidate_fp["domains"])
    search_alignment = max(
        (
            max(
                _phrase_similarity(keyword, job_item)
                for job_item in (job_fp["titles"] + job_fp["required_skills"] + job_fp["preferred_skills"])
            )
            for keyword in (search_keywords or [])
            if keyword.strip()
        ),
        default=0.0,
    )
    hard_blocker_penalty = 0.0
    if job_fp["hard_blockers"] and required_skill_overlap < 0.2:
        hard_blocker_penalty = 0.12
    local_pre_score = max(
        0.0,
        min(
            100.0,
            (
                role_alignment * 0.40
                + required_skill_overlap * 0.30
                + seniority_alignment * 0.10
                + search_alignment * 0.15
                + domain_alignment * 0.05
                - hard_blocker_penalty
            )
            * 100.0,
        ),
    )
    return {
        "role_alignment": role_alignment,
        "required_skill_overlap": required_skill_overlap,
        "preferred_skill_overlap": preferred_skill_overlap,
        "seniority_alignment": seniority_alignment,
        "search_alignment": search_alignment,
        "domain_alignment": domain_alignment,
        "hard_blocker_penalty": hard_blocker_penalty,
        "local_pre_score": local_pre_score,
        "matched_titles": candidate_fp["titles"][:4],
        "matched_skills": candidate_fp["skills"][:10],
        "job_required_skills": job_fp["required_skills"][:8],
        "job_hard_blockers": job_fp["hard_blockers"][:5],
    }


def _should_locally_reject(evidence: dict[str, Any]) -> bool:
    return (
        evidence["local_pre_score"] < 18.0
        and evidence["role_alignment"] < 0.18
        and evidence["search_alignment"] < 0.20
        and evidence["required_skill_overlap"] < 0.12
    )


async def _call_claude_json(
    client: anthropic.AsyncAnthropic,
    *,
    system: str,
    prompt: str,
    model: str,
    max_tokens: int,
    context: str,
) -> dict[str, Any]:
    response = await client.messages.create(
        model=model,
        system=system,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    return _safe_json_loads(raw, context=context)


async def _adjudicate_with_claude(
    *,
    client: anthropic.AsyncAnthropic,
    model: str,
    resume_text: str,
    job_title: str,
    job_description: str,
    candidate_fp: dict[str, Any],
    job_fp: dict[str, Any],
    search_keywords: list[str] | None,
    local_evidence: dict[str, Any],
    history_hints: list[dict[str, Any]],
) -> dict[str, Any]:
    system_prompt = """You are an industry-standard ATS reviewer, recruiter, and career consultant with 20+ years of hiring experience.

Judge fit quickly but accurately, like a senior recruiter doing a fast first-pass screen and then sanity-checking against the detailed evidence.

Rules:
- Be generous for genuinely transferable adjacent fit
- Do not over-penalize niche industry context unless the JD clearly makes it mandatory
- Treat the local pre-score as evidence, not ground truth
- Only list true blockers in missing_required_skills
- Use the full score range confidently
- Return ONLY valid JSON"""

    prompt = f"""Evaluate this candidate-job match.

<search_intent>
{", ".join(search_keywords or []) or "Not provided"}
</search_intent>

<candidate_profile_snapshot>
{candidate_fp["snapshot"] or "Not provided"}
</candidate_profile_snapshot>

<candidate_titles>
{json.dumps(candidate_fp["titles"], ensure_ascii=False)}
</candidate_titles>

<candidate_skills>
{json.dumps(candidate_fp["skills"][:18], ensure_ascii=False)}
</candidate_skills>

<candidate_domains>
{json.dumps(candidate_fp["domains"][:10], ensure_ascii=False)}
</candidate_domains>

<job_title>{job_title}</job_title>

<job_requirements>
{json.dumps(job_fp, ensure_ascii=False)}
</job_requirements>

<local_match_evidence>
{json.dumps(local_evidence, ensure_ascii=False)}
</local_match_evidence>

<history_hints>
{json.dumps(history_hints, ensure_ascii=False)}
</history_hints>

<resume_excerpt>
{resume_text[:3500]}
</resume_excerpt>

<job_description_excerpt>
{job_description[:4200]}
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
    return await _call_claude_json(
        client,
        system=system_prompt,
        prompt=prompt,
        model=model,
        max_tokens=650,
        context=f"match adjudication for {job_title}",
    )


async def score_compatibility(
    resume_text: str,
    job_title: str,
    job_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
    *,
    search_keywords: list[str] | None = None,
    profile: UserProfile | None = None,
    cache_backend: MatchCacheBackend | None = None,
    user_id: int | None = None,
    job_id: int | None = None,
) -> float:
    """
    Cost-optimized hybrid matcher:
    1. Deterministic local fingerprinting and pre-score
    2. Conservative local rejection of obvious mismatches
    3. One Claude adjudication only for plausible jobs
    4. DB-backed final result cache
    """
    if not job_description or not job_description.strip():
        logger.warning(f"No job description for '{job_title}' - defaulting score to 0")
        return 0.0

    candidate_fp = _build_candidate_fingerprint(resume_text, profile)
    job_fp = _build_job_fingerprint(job_title, job_description)
    local_evidence = _local_match_evidence(candidate_fp, job_fp, search_keywords)
    history_hints, history_similarity = await _get_history_hints(
        cache_backend=cache_backend,
        user_id=user_id,
        job_id=job_id,
        job_fp=job_fp,
    )

    profile_hash = _stable_hash(
        MATCHER_VERSION,
        candidate_fp["snapshot"],
        json.dumps(candidate_fp["titles"], ensure_ascii=False),
        json.dumps(candidate_fp["skills"][:20], ensure_ascii=False),
        json.dumps(candidate_fp["domains"][:12], ensure_ascii=False),
    )
    job_hash = _stable_hash(
        MATCHER_VERSION,
        job_title,
        job_description,
        json.dumps(job_fp, ensure_ascii=False),
    )
    search_hash = _stable_hash(
        MATCHER_VERSION,
        json.dumps(search_keywords or [], ensure_ascii=False),
    )
    cache_key = (
        f"match:{profile_hash[:16]}:{job_hash[:16]}:{search_hash[:12]}:{MATCHER_VERSION}"
    )
    source_hash = _stable_hash(profile_hash, job_hash, search_hash, MATCHER_VERSION)

    if cache_backend is not None:
        cached_match = await cache_backend.get_semantic_cache(cache_key, source_hash)
        if cached_match and "score" in cached_match:
            score = float(cached_match["score"])
            logger.info(
                f"Match score for '{job_title}': {score:.0f}% "
                f"(local={cached_match.get('local_pre_score', 0):.0f}%, "
                f"history={cached_match.get('history_similarity', 0):.0f}%, "
                f"mode={cached_match.get('mode', 'cache')}) | "
                f"missing: {cached_match.get('missing_required_skills', [])[:3]}"
            )
            return min(max(score, 0.0), 100.0)

    if _should_locally_reject(local_evidence):
        final_score = min(local_evidence["local_pre_score"], 25.0)
        payload = {
            "score": final_score,
            "local_pre_score": local_evidence["local_pre_score"],
            "history_similarity": history_similarity * 100.0,
            "mode": "local_reject",
            "missing_required_skills": job_fp["required_skills"][:3],
            "summary": "Rejected locally as an obvious mismatch before AI adjudication.",
        }
        if cache_backend is not None:
            await cache_backend.upsert_semantic_cache(
                cache_key,
                kind="match_score",
                source_hash=source_hash,
                payload=payload,
                user_id=user_id,
            )
        logger.info(
            f"Match score for '{job_title}': {final_score:.0f}% "
            f"(local={local_evidence['local_pre_score']:.0f}%, "
            f"history={history_similarity * 100.0:.0f}%, "
            f"mode=local_reject) | "
            f"missing: {payload['missing_required_skills']}"
        )
        return final_score

    try:
        adjudicated = await _adjudicate_with_claude(
            client=client,
            model=model,
            resume_text=resume_text,
            job_title=job_title,
            job_description=job_description,
            candidate_fp=candidate_fp,
            job_fp=job_fp,
            search_keywords=search_keywords,
            local_evidence=local_evidence,
            history_hints=history_hints,
        )
        ai_score = float(adjudicated.get("score", 0))
    except Exception as exc:
        logger.error(f"Error scoring '{job_title}': {exc}")
        final_score = min(local_evidence["local_pre_score"], 69.0)
        logger.info(
            f"Match score for '{job_title}': {final_score:.0f}% "
            f"(local={local_evidence['local_pre_score']:.0f}%, "
            f"history={history_similarity * 100.0:.0f}%, "
            f"mode=local_fallback)"
        )
        return final_score

    history_boost = min(history_similarity * 8.0, 4.0)
    final_score = min(
        max(ai_score * 0.85 + local_evidence["local_pre_score"] * 0.15 + history_boost, 0.0),
        100.0,
    )
    payload = {
        **adjudicated,
        "score": final_score,
        "ai_score": ai_score,
        "local_pre_score": local_evidence["local_pre_score"],
        "history_similarity": history_similarity * 100.0,
        "mode": "claude_adjudicated",
    }
    if cache_backend is not None:
        await cache_backend.upsert_semantic_cache(
            cache_key,
            kind="match_score",
            source_hash=source_hash,
            payload=payload,
            user_id=user_id,
        )

    missing = payload.get("missing_required_skills") or payload.get("missing_skills") or []
    logger.info(
        f"Match score for '{job_title}': {final_score:.0f}% "
        f"(ai={ai_score:.0f}%, "
        f"local={local_evidence['local_pre_score']:.0f}%, "
        f"history={history_similarity * 100.0:.0f}%, "
        f"mode=claude_adjudicated) | "
        f"missing: {missing[:3]}"
    )
    return final_score
