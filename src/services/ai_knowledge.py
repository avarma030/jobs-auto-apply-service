from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from src.models import Job, UserProfile
from src.services.ai_contracts import CandidateKnowledgePack, JobKnowledgePack
from src.services.application_questions import normalize_question_key, normalize_question_text


CANDIDATE_KNOWLEDGE_VERSION = "candidate-pack-v1"
JOB_KNOWLEDGE_VERSION = "job-pack-v1"
LOCAL_EMBEDDING_MODEL = "local-lexical-v1"

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


@dataclass(slots=True)
class AnswerMemoryHit:
    question_text: str
    answer_text: str
    source_kind: str
    confidence: float
    evidence: list[str]
    entry_id: int | None = None


def normalize_text(text: str | None) -> str:
    return re.sub(r"[^a-z0-9+#/.-]+", " ", (text or "").lower()).strip()


def stable_hash(*parts: str) -> str:
    digest = sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def tokenize(text: str | None) -> list[str]:
    return [
        token
        for token in normalize_text(text).split()
        if token and token not in _STOPWORDS
    ]


def dedupe_phrases(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    phrases: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        if not tokenize(normalized):
            continue
        seen.add(normalized)
        phrases.append(normalized)
        if len(phrases) >= limit:
            break
    return phrases


def split_fragments(text: str) -> list[str]:
    if not text:
        return []
    fragments: list[str] = []
    for line in text.splitlines():
        clean_line = line.strip(" \t-*•")
        if not clean_line:
            continue
        fragments.append(clean_line)
        for segment in re.split(r"[.;]", clean_line):
            segment = segment.strip(" \t-*•")
            if segment and segment != clean_line:
                fragments.append(segment)
    return fragments


def extract_marked_phrases(text: str, markers: tuple[str, ...], *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in split_fragments(text):
        lowered = fragment.lower()
        for marker in markers:
            idx = lowered.find(marker)
            if idx == -1:
                continue
            tail = fragment[idx + len(marker):].lstrip(" :-")
            if not tail:
                continue
            for part in re.split(r"[,/;|]| and | or ", tail):
                clean_part = part.strip(" \t-*•")
                token_count = len(tokenize(clean_part))
                if 2 <= token_count <= 8:
                    phrases.append(clean_part)
            if phrases:
                break
    return dedupe_phrases(phrases, limit=limit)


def extract_list_phrases(text: str, *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in split_fragments(text):
        if len(fragment) > 90:
            continue
        for part in re.split(r"[,/;|]", fragment):
            clean_part = part.strip(" \t-*•")
            token_count = len(tokenize(clean_part))
            if 2 <= token_count <= 7:
                phrases.append(clean_part)
    return dedupe_phrases(phrases, limit=limit)


def extract_domain_phrases(text: str, *, limit: int) -> list[str]:
    phrases: list[str] = []
    for fragment in split_fragments(text):
        lowered = fragment.lower()
        if any(marker in lowered for marker in _DOMAIN_MARKERS):
            for part in re.split(r"[,/;|]| and | or ", fragment):
                clean_part = part.strip(" \t-*•")
                token_count = len(tokenize(clean_part))
                if 2 <= token_count <= 6:
                    phrases.append(clean_part)
    return dedupe_phrases(phrases, limit=limit)


def extract_seniority(text: str) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    for seniority, markers in _SENIORITY_MARKERS:
        if any(marker in normalized for marker in markers):
            return seniority
    return None


def _token_weight(token: str) -> float:
    return 0.25 if token in _LOW_SIGNAL_TOKENS else 1.0


def phrase_similarity(left: str, right: str) -> float:
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    if normalize_text(left) == normalize_text(right):
        return 1.0
    left_weights = {token: _token_weight(token) for token in left_tokens}
    right_weights = {token: _token_weight(token) for token in right_tokens}
    union = set(left_weights) | set(right_weights)
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
    overlap = intersection_score / union_score
    left_distinctive = [token for token in left_tokens if token not in _LOW_SIGNAL_TOKENS]
    right_distinctive = [token for token in right_tokens if token not in _LOW_SIGNAL_TOKENS]
    if left_distinctive and set(left_distinctive).issubset(set(right_tokens)):
        overlap = max(overlap, 0.78)
    if right_distinctive and set(right_distinctive).issubset(set(left_tokens)):
        overlap = max(overlap, 0.78)
    if normalize_text(left) in normalize_text(right) or normalize_text(right) in normalize_text(left):
        overlap = max(overlap, 0.78)
    return overlap


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(x * y for x, y in zip(left, right))
    left_mag = sum(x * x for x in left) ** 0.5
    right_mag = sum(y * y for y in right) ** 0.5
    if not left_mag or not right_mag:
        return 0.0
    return dot / (left_mag * right_mag)


def embed_text(text: str, *, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokenize(text):
        slot = int(sha256(token.encode("utf-8")).hexdigest()[:8], 16) % dimensions
        vector[slot] += 1.0
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude:
        vector = [value / magnitude for value in vector]
    return vector


def _role_families_from_titles(titles: list[str]) -> list[str]:
    families: list[str] = []
    for title in titles:
        tokens = tokenize(title)
        if len(tokens) >= 2:
            families.append(" ".join(tokens[-2:]))
        if len(tokens) >= 3:
            families.append(" ".join(tokens[-3:]))
    return dedupe_phrases(families + titles, limit=10)


def _profile_snapshot(profile: UserProfile) -> str:
    recent_roles = [exp.title for exp in profile.work_experience[:5] if exp.title]
    skills = [skill for skill in profile.skills[:20] if skill]
    relevant_answers = []
    for question, answer in profile.custom_answers.items():
        normalized_question = normalize_text(question)
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


def _candidate_titles(profile: UserProfile) -> list[str]:
    titles: list[str] = []
    if profile.headline:
        titles.extend([part.strip() for part in re.split(r"[|,/]", profile.headline) if part.strip()])
    titles.extend(exp.title for exp in profile.work_experience[:6] if exp.title)
    return dedupe_phrases(titles, limit=12)


def _candidate_skills(profile: UserProfile, resume_text: str) -> list[str]:
    skills: list[str] = []
    skills.extend(profile.skills[:30])
    for question, answer in profile.custom_answers.items():
        normalized_question = normalize_text(question)
        if any(hint in normalized_question for hint in _RELEVANT_ANSWER_HINTS):
            skills.extend(re.split(r"[,/;|]", answer))
    skills.extend(extract_marked_phrases(resume_text[:2500], _REQUIRED_MARKERS, limit=10))
    skills.extend(extract_list_phrases(resume_text[:1800], limit=12))
    return dedupe_phrases(skills, limit=32)


def _candidate_domains(profile: UserProfile, resume_text: str) -> list[str]:
    text_chunks: list[str] = [resume_text[:1800]]
    if profile.summary:
        text_chunks.append(profile.summary)
    for exp in profile.work_experience[:5]:
        if exp.description:
            text_chunks.append(exp.description)
    domains: list[str] = []
    for chunk in text_chunks:
        domains.extend(extract_domain_phrases(chunk, limit=6))
        domains.extend(extract_list_phrases(chunk, limit=6))
    return dedupe_phrases(domains, limit=16)


def _candidate_evidence(profile: UserProfile, resume_text: str) -> list[str]:
    snippets: list[str] = []
    if profile.summary:
        snippets.append(profile.summary)
    for exp in profile.work_experience[:6]:
        fragments = [exp.title, exp.company, exp.location, exp.description or ""]
        snippet = " | ".join(part for part in fragments if part)
        if snippet:
            snippets.append(snippet)
    snippets.extend(split_fragments(resume_text[:2400])[:10])
    return dedupe_phrases(snippets, limit=18)


def _candidate_geography(profile: UserProfile) -> list[str]:
    values: list[str] = []
    if profile.address:
        if profile.address.city:
            values.append(profile.address.city)
        if profile.address.state:
            values.append(profile.address.state)
        if profile.address.country:
            values.append(profile.address.country)
    return dedupe_phrases(values, limit=6)


def _candidate_work_auth(profile: UserProfile) -> list[str]:
    hints: list[str] = []
    for question, answer in profile.custom_answers.items():
        lowered = normalize_text(question)
        answer_text = normalize_text(answer)
        if "authorized" in lowered or "sponsorship" in lowered or "visa" in lowered:
            hints.append(f"{question}: {answer_text}")
    return dedupe_phrases(hints, limit=6)


def _job_title_variants(title: str) -> list[str]:
    return dedupe_phrases([title] + re.split(r"[-|,/]", title), limit=8)


def _job_evidence(job: Job) -> list[str]:
    fragments = [job.title, job.location or "", job.description or ""]
    snippets: list[str] = []
    for fragment in fragments:
        if not fragment:
            continue
        snippets.extend(split_fragments(fragment)[:10])
    return dedupe_phrases(snippets, limit=16)


def _job_work_modes(job: Job) -> list[str]:
    values: list[str] = []
    if getattr(job, "work_mode", None):
        values.append(str(job.work_mode))
    description = job.description or ""
    for candidate in ("remote", "hybrid", "onsite", "on-site"):
        if candidate in description.lower():
            values.append(candidate.replace("-", ""))
    return dedupe_phrases(values, limit=4)


def _job_compensation(job: Job) -> list[str]:
    parts: list[str] = []
    if job.salary_min is not None or job.salary_max is not None:
        currency = job.salary_currency or ""
        parts.append(f"{currency} {job.salary_min or ''} {job.salary_max or ''}".strip())
    return dedupe_phrases(parts, limit=2)


def build_candidate_knowledge_pack(profile: UserProfile, resume_text: str) -> CandidateKnowledgePack:
    snapshot = _profile_snapshot(profile)
    titles = _candidate_titles(profile)
    skills = _candidate_skills(profile, resume_text)
    domains = _candidate_domains(profile, resume_text)
    evidence_snippets = _candidate_evidence(profile, resume_text)
    source_hash = stable_hash(
        CANDIDATE_KNOWLEDGE_VERSION,
        snapshot,
        json.dumps(titles, ensure_ascii=False),
        json.dumps(skills, ensure_ascii=False),
        json.dumps(domains, ensure_ascii=False),
        json.dumps(profile.custom_answers, ensure_ascii=False, sort_keys=True),
        resume_text[:6000],
    )
    embedding = embed_text(
        " ".join(
            [
                snapshot,
                " ".join(titles),
                " ".join(skills[:20]),
                " ".join(domains[:12]),
                " ".join(evidence_snippets[:8]),
            ]
        )
    )
    return CandidateKnowledgePack(
        version=CANDIDATE_KNOWLEDGE_VERSION,
        source_hash=source_hash,
        snapshot=snapshot,
        titles=titles,
        role_families=_role_families_from_titles(titles),
        skills=skills,
        domains=domains,
        industries=domains[:8],
        geography=_candidate_geography(profile),
        work_authorization=_candidate_work_auth(profile),
        seniority=extract_seniority(" ".join(titles)),
        years_of_experience=profile.years_of_experience,
        custom_answers={
            normalize_question_text(question): answer
            for question, answer in profile.custom_answers.items()
            if normalize_question_text(question) and answer.strip()
        },
        evidence_snippets=evidence_snippets,
        embedding=embedding,
        embedding_model=LOCAL_EMBEDDING_MODEL,
    )


def build_job_knowledge_pack(job: Job) -> JobKnowledgePack:
    description = job.description or ""
    required_skills = extract_marked_phrases(description, _REQUIRED_MARKERS, limit=16)
    preferred_skills = extract_marked_phrases(description, _PREFERRED_MARKERS, limit=12)
    if not required_skills:
        required_skills = extract_list_phrases(description[:2400], limit=12)
    titles = _job_title_variants(job.title)
    domains = extract_domain_phrases(f"{job.title}\n{description[:2400]}", limit=10)
    hard_blockers = extract_marked_phrases(description, _HARD_BLOCKER_MARKERS, limit=8)
    source_hash = stable_hash(
        JOB_KNOWLEDGE_VERSION,
        job.title,
        job.location or "",
        description[:7000],
        json.dumps(job.skills, ensure_ascii=False),
    )
    evidence_snippets = _job_evidence(job)
    embedding = embed_text(
        " ".join(
            [
                job.title,
                description[:2500],
                " ".join(required_skills[:10]),
                " ".join(preferred_skills[:10]),
            ]
        )
    )
    return JobKnowledgePack(
        version=JOB_KNOWLEDGE_VERSION,
        source_hash=source_hash,
        title=job.title,
        location=job.location,
        titles=titles,
        role_families=_role_families_from_titles(titles),
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        domains=domains,
        industries=domains[:8],
        hard_blockers=hard_blockers,
        work_modes=_job_work_modes(job),
        compensation=_job_compensation(job),
        seniority=extract_seniority(f"{job.title} {description[:400]}"),
        evidence_snippets=evidence_snippets,
        embedding=embedding,
        embedding_model=LOCAL_EMBEDDING_MODEL,
    )


async def ensure_candidate_knowledge_pack(
    db,
    *,
    user_id: int | None,
    profile: UserProfile,
    resume_text: str,
) -> CandidateKnowledgePack:
    pack = build_candidate_knowledge_pack(profile, resume_text)
    if user_id is None:
        return pack
    cached = await db.get_candidate_knowledge_pack(
        user_id,
        version=pack.version,
        source_hash=pack.source_hash,
    )
    if cached is not None:
        return CandidateKnowledgePack.model_validate(
            {
                **cached,
                "embedding": cached.get("_embedding_json") or cached.get("embedding") or [],
                "embedding_model": cached.get("_embedding_model") or cached.get("embedding_model"),
                "source_hash": cached.get("_source_hash") or cached.get("source_hash"),
                "version": cached.get("_version") or cached.get("version"),
            }
        )
    await db.upsert_candidate_knowledge_pack(
        user_id,
        version=pack.version,
        source_hash=pack.source_hash,
        payload=pack.model_dump(mode="json", exclude={"embedding", "embedding_model"}),
        embedding=pack.embedding,
        embedding_model=pack.embedding_model,
    )
    return pack


async def ensure_job_knowledge_pack(
    db,
    *,
    job_id: int,
    user_id: int | None,
    job: Job,
) -> JobKnowledgePack:
    pack = build_job_knowledge_pack(job)
    cached = await db.get_job_knowledge_pack(
        job_id,
        version=pack.version,
        source_hash=pack.source_hash,
    )
    if cached is not None:
        return JobKnowledgePack.model_validate(
            {
                **cached,
                "embedding": cached.get("_embedding_json") or cached.get("embedding") or [],
                "embedding_model": cached.get("_embedding_model") or cached.get("embedding_model"),
                "source_hash": cached.get("_source_hash") or cached.get("source_hash"),
                "version": cached.get("_version") or cached.get("version"),
            }
        )
    await db.upsert_job_knowledge_pack(
        job_id,
        user_id=user_id,
        version=pack.version,
        source_hash=pack.source_hash,
        payload=pack.model_dump(mode="json", exclude={"embedding", "embedding_model"}),
        embedding=pack.embedding,
        embedding_model=pack.embedding_model,
    )
    return pack


async def sync_profile_answer_memory(
    db,
    *,
    user_id: int | None,
    profile: UserProfile,
) -> None:
    if user_id is None:
        return
    for question, answer in profile.custom_answers.items():
        normalized_question = normalize_question_text(question)
        if not normalized_question or not str(answer).strip():
            continue
        await db.upsert_answer_memory(
            user_id=user_id,
            question_key=normalize_question_key(normalized_question),
            question_text=normalized_question,
            answer_text=str(answer).strip(),
            source_kind="profile",
            confidence=0.98,
            approved=True,
            evidence={"source": "profile.custom_answers"},
            embedding=embed_text(normalized_question),
            embedding_model=LOCAL_EMBEDDING_MODEL,
        )


def select_evidence_snippets(
    evidence_snippets: list[str],
    query_text: str,
    *,
    limit: int = 4,
) -> list[str]:
    query_vector = embed_text(query_text)
    ranked = sorted(
        evidence_snippets,
        key=lambda snippet: (
            cosine_similarity(embed_text(snippet), query_vector)
            + phrase_similarity(query_text, snippet) * 0.35
        ),
        reverse=True,
    )
    return [snippet for snippet in ranked[:limit] if snippet]


def _memory_option_compatible(answer: str, options: list[str]) -> bool:
    if not options:
        return True
    normalized_answer = normalize_text(answer)
    normalized_options = [normalize_text(option) for option in options]
    if normalized_answer in normalized_options:
        return True
    return any(phrase_similarity(answer, option) >= 0.92 for option in options)


async def resolve_answer_memory(
    db,
    *,
    user_id: int | None,
    prompts: list[dict[str, Any]],
    candidate_pack: CandidateKnowledgePack | None,
) -> dict[str, AnswerMemoryHit]:
    if user_id is None or db is None or not prompts:
        return {}
    exact_keys = [
        normalize_question_key(prompt.get("question"))
        for prompt in prompts
        if prompt.get("question")
    ]
    memories = await db.get_answer_memory_entries(user_id=user_id, limit=120)
    hits: dict[str, AnswerMemoryHit] = {}
    used_ids: list[int] = []
    for prompt in prompts:
        question = normalize_question_text(prompt.get("question"))
        if not question:
            continue
        options = [str(option).strip() for option in prompt.get("options", []) if str(option).strip()]
        prompt_key = normalize_question_key(question)
        best_entry: dict[str, Any] | None = None
        best_score = 0.0
        prompt_vector = embed_text(question)
        for entry in memories:
            if not entry.get("approved", True):
                continue
            answer_text = str(entry.get("answer_text") or "").strip()
            if not answer_text or not _memory_option_compatible(answer_text, options):
                continue
            similarity = 0.0
            if entry.get("question_key") == prompt_key:
                similarity = 1.0
            else:
                similarity = max(
                    phrase_similarity(question, str(entry.get("question_text") or "")),
                    cosine_similarity(entry.get("embedding") or [], prompt_vector),
                )
            confidence = float(entry.get("confidence") or 0.0)
            total_score = similarity * 0.75 + confidence * 0.25
            if total_score > best_score:
                best_score = total_score
                best_entry = entry
        if best_entry is None:
            continue
        if best_entry.get("question_key") != prompt_key and best_score < 0.9:
            continue
        evidence = best_entry.get("evidence")
        evidence_list = evidence if isinstance(evidence, list) else []
        if not evidence_list and isinstance(evidence, dict):
            evidence_list = [str(value) for value in evidence.values() if str(value).strip()]
        if candidate_pack is not None:
            evidence_list = (
                evidence_list
                or select_evidence_snippets(candidate_pack.evidence_snippets, question, limit=2)
            )
        hits[question] = AnswerMemoryHit(
            question_text=question,
            answer_text=str(best_entry["answer_text"]),
            source_kind=str(best_entry.get("source_kind") or "memory"),
            confidence=min(max(best_score, 0.0), 1.0),
            evidence=evidence_list[:3],
            entry_id=best_entry.get("id"),
        )
        if best_entry.get("id") is not None:
            used_ids.append(int(best_entry["id"]))
    if used_ids:
        await db.mark_answer_memory_used(used_ids)
    return hits


async def store_answer_memory(
    db,
    *,
    user_id: int | None,
    question_text: str,
    answer_text: str,
    source_kind: str,
    confidence: float = 1.0,
    approved: bool = True,
    evidence: list[str] | dict | None = None,
) -> None:
    normalized_question = normalize_question_text(question_text)
    if user_id is None or db is None or not normalized_question or not str(answer_text).strip():
        return
    await db.upsert_answer_memory(
        user_id=user_id,
        question_key=normalize_question_key(normalized_question),
        question_text=normalized_question,
        answer_text=str(answer_text).strip(),
        source_kind=source_kind,
        confidence=confidence,
        approved=approved,
        evidence=evidence,
        embedding=embed_text(normalized_question),
        embedding_model=LOCAL_EMBEDDING_MODEL,
    )


def load_resume_text_from_profile(profile: UserProfile) -> str:
    resume_path = getattr(profile, "resume_path", None)
    if not resume_path:
        return ""
    path = Path(resume_path)
    if not path.exists():
        return ""
    try:
        from src.services.resume_parser import parse_resume

        return parse_resume(path)
    except Exception:
        return ""

