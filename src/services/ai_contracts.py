from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CandidateKnowledgePack(BaseModel):
    version: str
    source_hash: str
    snapshot: str = ""
    titles: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    geography: list[str] = Field(default_factory=list)
    work_authorization: list[str] = Field(default_factory=list)
    seniority: str | None = None
    years_of_experience: int | None = None
    custom_answers: dict[str, str] = Field(default_factory=dict)
    evidence_snippets: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    embedding_model: str | None = None


class JobKnowledgePack(BaseModel):
    version: str
    source_hash: str
    title: str
    location: str | None = None
    titles: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    hard_blockers: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    compensation: list[str] = Field(default_factory=list)
    seniority: str | None = None
    evidence_snippets: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    embedding_model: str | None = None


class ProfileExtractionResult(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    headline: str | None = None
    summary: str | None = None
    years_of_experience: int | None = None
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    work_experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    social_links: dict[str, str | None] = Field(default_factory=dict)
    address: dict[str, str | None] = Field(default_factory=dict)


class MatchDecisionResult(BaseModel):
    score: float = 0.0
    skills_match: float = 0.0
    experience_match: float = 0.0
    domain_match: float = 0.0
    education_match: float = 0.0
    top_matching_skills: list[str] = Field(default_factory=list)
    missing_required_skills: list[str] = Field(default_factory=list)
    missing_nice_to_have: list[str] = Field(default_factory=list)
    summary: str = ""


class ScreeningAnswerItem(BaseModel):
    question: str
    answer: str
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class ScreeningAnswerBatchResult(BaseModel):
    answers: list[ScreeningAnswerItem] = Field(default_factory=list)


class ATSScoreResult(BaseModel):
    ats_score: float = 0.0
    keyword_matches: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    recommendation: str = ""


class TailoredResumeResult(BaseModel):
    tailored_resume_text: str
    change_summary: list[str] = Field(default_factory=list)


class CoverLetterResult(BaseModel):
    cover_letter: str

