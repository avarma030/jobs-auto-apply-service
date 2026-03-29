from __future__ import annotations

from src.models.job import Job

# Map of ATS slug → URL substrings that indicate that platform
_ATS_PATTERNS: dict[str, list[str]] = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse.io"],
    "workday": ["myworkdayjobs.com", "wd1.myworkdayjobs.com", "wd3.myworkdayjobs.com"],
    "lever": ["jobs.lever.co", "lever.co"],
    "icims": ["jobs.icims.com", "icims.com/jobs"],
    "taleo": ["taleo.net", "tbe.taleo.net"],
    "smartrecruiters": ["jobs.smartrecruiters.com", "smartrecruiters.com"],
    "ashby": ["jobs.ashbyhq.com", "ashbyhq.com"],
    "bamboohr": ["bamboohr.com/careers"],
    "jobvite": ["jobs.jobvite.com", "hire.jobvite.com"],
    "successfactors": ["successfactors.com", "sapsf.com"],
}


def detect_ats(job: Job) -> str:
    """
    Infer ATS platform from job URL or source board.
    Returns an ATS slug (e.g. 'greenhouse') or 'linkedin' / 'generic'.
    """
    url = (job.url or "").lower()
    board = (job.source_board or "").lower()

    # Fast-path: already known board
    if board == "linkedin":
        return "linkedin"

    for slug, patterns in _ATS_PATTERNS.items():
        for pattern in patterns:
            if pattern in url:
                return slug

    # Fall back to board name if recognised
    if board in _ATS_PATTERNS:
        return board

    return "generic"


def is_easy_apply(job: Job) -> bool:
    """Return True if the job supports LinkedIn Easy Apply."""
    return bool(job.easy_apply) or detect_ats(job) == "linkedin"
