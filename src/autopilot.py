from __future__ import annotations

import json
import re
import zipfile
from html import escape as escape_xml
from pathlib import Path
from typing import Iterable, Type

from loguru import logger

from src.appliers.base import BaseApplier
from src.appliers.generic import GenericApplier
from src.appliers.greenhouse import GreenhouseApplier
from src.appliers.lever import LeverApplier
from src.appliers.linkedin import LinkedInApplier
from src.appliers.workday import WorkdayApplier
from src.models import (
    ApplicationPackage,
    ApplicationRoute,
    ApplicationStatus,
    AutopilotRun,
    AutomationState,
    Job,
    JobAutomationResult,
    JobSearchFilter,
    UserProfile,
    WorkMode,
)
from src.scrapers.base import BaseScraper

DEFAULT_LIVE_APPLY_ROUTES: set[ApplicationRoute] = set()
ROUTE_HANDLER_NAMES = {
    ApplicationRoute.LINKEDIN_EASY_APPLY: "LinkedInApplier",
    ApplicationRoute.LINKEDIN_EXTERNAL: "ExternalSiteHandler",
    ApplicationRoute.GREENHOUSE: "GreenhouseApplier",
    ApplicationRoute.WORKDAY: "WorkdayApplier",
    ApplicationRoute.LEVER: "LeverApplier",
    ApplicationRoute.GENERIC: "GenericApplier",
}
DEFAULT_APPLIERS: list[Type[BaseApplier]] = [
    LinkedInApplier,
    GreenhouseApplier,
    LeverApplier,
    WorkdayApplier,
    GenericApplier,
]
STOPWORDS = {
    "able",
    "about",
    "across",
    "after",
    "again",
    "all",
    "already",
    "also",
    "among",
    "and",
    "any",
    "around",
    "based",
    "because",
    "been",
    "before",
    "being",
    "better",
    "build",
    "building",
    "care",
    "can",
    "core",
    "company",
    "cross",
    "data",
    "deliver",
    "delivery",
    "driven",
    "each",
    "engineers",
    "experience",
    "focused",
    "from",
    "full",
    "great",
    "hands",
    "help",
    "high",
    "into",
    "job",
    "jobs",
    "lead",
    "looking",
    "make",
    "more",
    "must",
    "need",
    "other",
    "our",
    "over",
    "own",
    "products",
    "product",
    "strong",
    "role",
    "team",
    "their",
    "them",
    "there",
    "these",
    "they",
    "through",
    "this",
    "than",
    "while",
    "using",
    "various",
    "with",
    "work",
    "working",
    "you",
    "your",
    "the",
}
TITLE_LEVEL_REQUIREMENTS = {
    "entry": 1,
    "mid": 3,
    "senior": 5,
    "lead": 7,
    "executive": 10,
}
EXCLUSION_MESSAGES = {
    "linkedin_external": "High-fit LinkedIn job excluded from the active list because it uses an external apply flow.",
    "easy_apply_unconfirmed": "High-fit LinkedIn job excluded because Easy Apply could not be confirmed after detail enrichment.",
}


class AutopilotEngine:
    """Runs the scrape -> score -> tailor -> route -> apply pipeline."""

    def __init__(
        self,
        profile: UserProfile,
        *,
        applier_classes: list[Type[BaseApplier]] | None = None,
        live_apply_routes: set[ApplicationRoute] | None = None,
        artifact_dir: Path | None = None,
        min_match_score: int | None = None,
        min_ats_score: int | None = None,
    ) -> None:
        self.profile = profile
        self.applier_classes = applier_classes or DEFAULT_APPLIERS
        self.live_apply_routes = live_apply_routes or set(DEFAULT_LIVE_APPLY_ROUTES)
        self.artifact_dir = artifact_dir or Path("data/generated")
        self.min_match_score = (
            profile.preferences.min_match_score if min_match_score is None else min_match_score
        )
        self.min_ats_score = (
            profile.preferences.min_ats_score if min_ats_score is None else min_ats_score
        )
        self._open_appliers: dict[str, BaseApplier] = {}
        self._profile_title_terms = ordered_terms(self.profile.headline or "")
        self._profile_summary_terms = ordered_terms(self.profile.summary or "")
        self._profile_skill_terms = ordered_terms(" ".join(self.profile.skills))
        self._profile_experience_terms = ordered_terms(
            *[
                " ".join(
                    filter(
                        None,
                        [
                            experience.title,
                            experience.company,
                            experience.description or "",
                            experience.location or "",
                        ],
                    )
                )
                for experience in self.profile.work_experience
            ]
        )
        self._profile_terms = unique_in_order(
            [
                *self._profile_title_terms,
                *self._profile_skill_terms,
                *self._profile_experience_terms,
                *self._profile_summary_terms,
            ]
        )
        self._profile_term_set = {term.lower() for term in self._profile_terms}
        self._profile_role_term_set = {
            term.lower()
            for term in unique_in_order(
                [
                    *self._profile_title_terms,
                    *self._profile_skill_terms,
                    *self._profile_experience_terms,
                ]
            )
        }
        self._profile_skill_term_set = {
            term.lower()
            for term in unique_in_order(
                [
                    *self._profile_skill_terms,
                    *self._profile_experience_terms,
                ]
            )
        }

    async def run_search(
        self,
        *,
        board: str,
        scraper_cls: Type[BaseScraper],
        search_filter: JobSearchFilter,
        limit: int = 10,
        scan_cap: int | None = None,
    ) -> AutopilotRun:
        counts = _RunCounts()
        results: list[JobAutomationResult] = []
        excluded_results: list[JobAutomationResult] = []
        max_candidates = (
            max(limit, scan_cap)
            if scan_cap is not None
            else max(limit * 4, limit + 5)
        )

        try:
            async with scraper_cls() as scraper:
                async for job in scraper.search(search_filter):
                    counts.total_scraped += 1
                    prepared_job = await self._prepare_job_for_evaluation(
                        job,
                        search_filter,
                        scraper=scraper,
                    )
                    evaluation = await self._evaluate_job(prepared_job, search_filter)
                    if evaluation is None:
                        counts.filtered_out_count += 1
                    else:
                        if evaluation.excluded_from_active_list:
                            excluded_results.append(evaluation)
                        else:
                            results.append(evaluation)
                        counts.capture(evaluation)
                        if len(results) >= limit:
                            break

                    if counts.total_scraped >= max_candidates and len(results) < limit:
                        logger.info(
                            f"Autopilot stopped after scanning {counts.total_scraped} jobs on {board} "
                            "to keep the dashboard responsive"
                        )
                        break
        finally:
            await self._close_open_appliers()

        return self._finalize_run(board, results, excluded_results, counts)

    async def process_jobs(
        self,
        jobs: Iterable[Job],
        *,
        board: str,
        search_filter: JobSearchFilter,
        limit: int | None = None,
    ) -> AutopilotRun:
        counts = _RunCounts()
        results: list[JobAutomationResult] = []
        excluded_results: list[JobAutomationResult] = []

        try:
            for job in jobs:
                counts.total_scraped += 1
                evaluation = await self._evaluate_job(job, search_filter)
                if evaluation is None:
                    counts.filtered_out_count += 1
                    continue

                if evaluation.excluded_from_active_list:
                    excluded_results.append(evaluation)
                else:
                    results.append(evaluation)
                counts.capture(evaluation)
                if limit is not None and len(results) >= limit:
                    break
        finally:
            await self._close_open_appliers()

        return self._finalize_run(board, results, excluded_results, counts)

    async def _evaluate_job(
        self,
        job: Job,
        search_filter: JobSearchFilter,
    ) -> JobAutomationResult | None:
        if self._is_company_filtered(job):
            return None

        compatibility_score, matched_keywords, missing_keywords, reasons = self._score_job(
            job,
            search_filter,
        )
        if compatibility_score < self.min_match_score:
            return None

        route = detect_application_route(job)
        if job.source_board == "linkedin" and route != ApplicationRoute.LINKEDIN_EASY_APPLY:
            return self._build_excluded_result(
                job,
                compatibility_score=compatibility_score,
                reasons=reasons,
                route=route,
            )

        package = self._build_application_package(
            job,
            matched_keywords=matched_keywords,
            missing_keywords=missing_keywords,
            compatibility_score=compatibility_score,
        )
        result = JobAutomationResult(
            job=job,
            compatibility_score=compatibility_score,
            compatibility_reasons=reasons,
            route=route,
            handler_name=ROUTE_HANDLER_NAMES[route],
            package=package,
        )

        if package.ats_score < self.min_ats_score:
            result.automation_state = AutomationState.NEEDS_REVIEW
            result.auto_apply_message = (
                f"Tailored resume reached {package.ats_score}% ATS alignment, below the "
                f"{self.min_ats_score}% apply threshold."
            )
            return result

        if not self.profile.preferences.auto_apply:
            result.auto_apply_message = "Auto-apply is disabled in the user profile."
            return result

        if self.profile.preferences.easy_apply_only and route != ApplicationRoute.LINKEDIN_EASY_APPLY:
            return None

        if route not in self.live_apply_routes:
            result.automation_state = AutomationState.QUEUED
            result.auto_apply_message = (
                f"Tailored application package is ready and routed to {result.handler_name}."
            )
            return result

        try:
            applier = await self._get_open_applier(job)
            application_result = await applier.apply(job, package=package)
        except Exception as exc:
            result.automation_state = AutomationState.FAILED
            result.application_status = ApplicationStatus.FAILED
            result.auto_apply_message = str(exc)
            return result

        result.application_status = application_result.status
        result.confirmation_id = application_result.confirmation_id
        result.auto_apply_message = application_result.message or self._default_apply_message(
            result.handler_name,
            application_result.status,
        )
        if application_result.status == ApplicationStatus.APPLIED:
            result.automation_state = AutomationState.APPLIED
        elif application_result.status == ApplicationStatus.FAILED:
            result.automation_state = AutomationState.FAILED
        else:
            result.automation_state = AutomationState.NEEDS_REVIEW
        return result

    async def _prepare_job_for_evaluation(
        self,
        job: Job,
        search_filter: JobSearchFilter,
        *,
        scraper: BaseScraper,
    ) -> Job:
        if job.source_board != "linkedin" or job.description:
            return job

        if not self._should_enrich_linkedin_job(job, search_filter):
            return job

        try:
            return await scraper.get_job_details(job)
        except Exception as exc:
            logger.warning(f"[LinkedIn] Failed to enrich job {job.external_id or job.url}: {exc}")
            mark_uncertain = getattr(scraper, "mark_easy_apply_uncertain", None)
            if callable(mark_uncertain):
                return mark_uncertain(job, f"detail enrichment failed: {exc}")
            return job

    def _should_enrich_linkedin_job(
        self,
        job: Job,
        search_filter: JobSearchFilter,
    ) -> bool:
        preliminary_score, _, _, _ = self._score_job(job, search_filter)
        search_terms = ordered_terms(" ".join(search_filter.keywords))
        title_terms = {term.lower() for term in ordered_terms(job.title)}
        matched_title_terms = {
            term.lower() for term in search_terms if term.lower() in title_terms
        }
        has_phrase_overlap = any(
            keyword.strip() and keyword.lower() in job.title.lower()
            for keyword in search_filter.keywords
        )
        detail_fetch_floor = max(50, self.min_match_score - 15)

        return (
            job.easy_apply
            or preliminary_score >= detail_fetch_floor
            or has_phrase_overlap
            or (has_ai_concept(search_terms) and has_ai_concept(title_terms))
            or len(matched_title_terms) >= max(1, min(2, len(search_terms)))
        )

    def _build_excluded_result(
        self,
        job: Job,
        *,
        compatibility_score: int,
        reasons: list[str],
        route: ApplicationRoute,
    ) -> JobAutomationResult:
        exclusion_reason = (
            "easy_apply_unconfirmed"
            if not getattr(job, "easy_apply_confident", True)
            else "linkedin_external"
        )
        return JobAutomationResult(
            job=job,
            compatibility_score=compatibility_score,
            compatibility_reasons=reasons,
            route=route,
            handler_name=ROUTE_HANDLER_NAMES[route],
            auto_apply_message=EXCLUSION_MESSAGES[exclusion_reason],
            excluded_from_active_list=True,
            exclusion_reason=exclusion_reason,
        )

    async def _get_open_applier(self, job: Job) -> BaseApplier:
        for applier_cls in self.applier_classes:
            probe = applier_cls(profile=self.profile)
            if not probe.can_apply(job):
                continue

            key = probe.board_slug or probe.__class__.__name__
            existing = self._open_appliers.get(key)
            if existing is not None:
                return existing

            await probe.setup()
            self._open_appliers[key] = probe
            return probe

        fallback = GenericApplier(profile=self.profile)
        key = fallback.board_slug or fallback.__class__.__name__
        existing = self._open_appliers.get(key)
        if existing is not None:
            return existing
        await fallback.setup()
        self._open_appliers[key] = fallback
        return fallback

    async def _close_open_appliers(self) -> None:
        for applier in list(self._open_appliers.values()):
            try:
                await applier.teardown()
            except Exception as exc:
                logger.debug(f"Failed to tear down {applier.__class__.__name__}: {exc}")
        self._open_appliers.clear()

    def _is_company_filtered(self, job: Job) -> bool:
        company = job.company.strip().lower()
        if not company:
            return False

        blacklist = {name.strip().lower() for name in self.profile.preferences.blacklisted_companies}
        whitelist = {name.strip().lower() for name in self.profile.preferences.whitelisted_companies}
        if company in blacklist:
            return True
        if whitelist and company not in whitelist:
            return True
        return False

    def _score_job(
        self,
        job: Job,
        search_filter: JobSearchFilter,
    ) -> tuple[int, list[str], list[str], list[str]]:
        search_phrases = [keyword.lower() for keyword in search_filter.keywords if keyword.strip()]
        title_text = job.title.lower()
        searchable_text = " ".join(
            filter(
                None,
                [
                    job.title,
                    job.description or "",
                    " ".join(job.skills),
                    " ".join(job.tags),
                    job.location or "",
                    job.company,
                ],
            )
        ).lower()
        search_terms = ordered_terms(" ".join(search_filter.keywords))
        job_title_terms = ordered_terms(job.title)
        job_skill_terms = ordered_terms(" ".join(job.skills), " ".join(job.tags))
        job_description_terms = ordered_terms(job.description or "")
        job_terms = unique_in_order([*job_title_terms, *job_skill_terms, *job_description_terms])
        search_term_set = {term.lower() for term in search_terms}
        job_title_term_set = {term.lower() for term in job_title_terms}
        job_skill_term_set = {term.lower() for term in job_skill_terms}
        job_description_term_set = {term.lower() for term in job_description_terms}

        matched_keywords = unique_in_order(
            [term for term in job_terms if term.lower() in self._profile_term_set]
        )[:8]
        missing_keywords = unique_in_order(
            [term for term in job_terms if term.lower() not in self._profile_term_set]
        )[:6]

        search_signal = 0.0
        for phrase in search_phrases:
            if phrase in title_text:
                search_signal += 2.0
            elif phrase in searchable_text:
                search_signal += 1.0
        for term in search_terms:
            if term in job_title_terms:
                search_signal += 1.0
            elif term in searchable_text:
                search_signal += 0.5
        search_denominator = max(1.0, (len(search_phrases) * 2) + len(search_terms))
        search_score = min(20, int(round((search_signal / search_denominator) * 20)))

        title_matches = [term for term in job_title_terms if term.lower() in self._profile_role_term_set]
        title_score = 0
        if job_title_terms:
            title_score = min(
                30,
                int(round((len(title_matches) / max(1, min(len(job_title_terms), 4))) * 30)),
            )

        skill_matches = [term for term in job_skill_terms if term.lower() in self._profile_skill_term_set]
        skill_score = 0
        if job_skill_terms:
            skill_score = min(
                20,
                int(round((len(skill_matches) / max(1, min(len(job_skill_terms), 6))) * 20)),
            )

        description_focus_terms = [
            term for term in job_description_terms if term.lower() not in {token.lower() for token in job_title_terms}
        ]
        description_matches = [
            term for term in description_focus_terms if term.lower() in self._profile_term_set
        ]
        description_score = 0
        if description_focus_terms:
            description_score = min(
                15,
                int(
                    round(
                        (len(description_matches) / max(1, min(len(description_focus_terms), 12)))
                        * 15
                    )
                ),
            )

        concept_score = 0
        if search_terms and job_title_term_set:
            matched_query_terms = sum(1 for term in search_term_set if term in job_title_term_set)
            if matched_query_terms == len(search_term_set):
                concept_score += 4
            elif matched_query_terms >= max(1, len(search_term_set) - 1):
                concept_score += 2

        if has_ai_concept(search_term_set) and (
            has_ai_concept(job_title_term_set)
            or has_ai_concept(job_skill_term_set)
            or has_ai_concept(job_description_term_set)
        ):
            concept_score += 3

        if has_ai_concept(self._profile_role_term_set) and (
            has_ai_concept(job_title_term_set)
            or has_ai_concept(job_skill_term_set)
            or has_ai_concept(job_description_term_set)
        ):
            concept_score += 3

        concept_score = min(10, concept_score)
        experience_score = self._experience_score(job)
        preference_score = self._preference_score(job, search_filter)
        total_score = min(
            100,
            search_score
            + title_score
            + skill_score
            + description_score
            + concept_score
            + experience_score
            + preference_score,
        )
        reasons = [
            f"{search_score}% search alignment",
            f"{title_score}% role alignment",
            f"{skill_score}% explicit skill alignment",
            f"{description_score}% description support",
        ]
        if concept_score:
            reasons.append(f"{concept_score}% concept alignment")
        if experience_score:
            reasons.append(f"{experience_score}% experience alignment")
        if preference_score:
            reasons.append(f"{preference_score}% preference fit")
        return total_score, matched_keywords, missing_keywords, reasons

    def _experience_score(self, job: Job) -> int:
        if job.experience_level is None:
            return 7

        level = str(job.experience_level)
        required_years = TITLE_LEVEL_REQUIREMENTS.get(level, 0)
        candidate_years = self.profile.years_of_experience or 0
        if candidate_years >= required_years:
            return 10
        if candidate_years + 1 >= required_years:
            return 7
        if candidate_years + 2 >= required_years:
            return 4
        return 1

    def _preference_score(self, job: Job, search_filter: JobSearchFilter) -> int:
        score = 0
        preferred_modes = {mode.lower() for mode in self.profile.preferences.preferred_work_modes}
        if job.work_mode is not None and str(job.work_mode).lower() in preferred_modes:
            score += 3
        location = (job.location or "").lower()
        if search_filter.remote_only and job.work_mode == WorkMode.REMOTE:
            score += 2
        elif search_filter.location and search_filter.location.lower() in location:
            score += 2
        return min(score, 5)

    def _build_application_package(
        self,
        job: Job,
        *,
        matched_keywords: list[str],
        missing_keywords: list[str],
        compatibility_score: int,
    ) -> ApplicationPackage:
        slug = slugify(f"{job.company}-{job.title}-{job.external_id or job.url}")[:64]
        job_dir = self.artifact_dir / job.source_board / slug
        job_dir.mkdir(parents=True, exist_ok=True)

        added_keywords = missing_keywords[:3]
        keyword_lift = min(18, len(matched_keywords) + len(added_keywords) * 3)
        ats_score = min(98, compatibility_score + keyword_lift)
        resume_preview = build_tailored_resume_preview(
            self.profile,
            job,
            matched_keywords=matched_keywords,
            added_keywords=added_keywords,
        )
        cover_letter_text = build_tailored_cover_letter(
            self.profile,
            job,
            matched_keywords=matched_keywords,
            added_keywords=added_keywords,
        )

        resume_path = job_dir / "resume.docx"
        cover_letter_path = job_dir / "cover-letter.txt"
        write_simple_docx(
            resume_path,
            lines=[line for line in resume_preview.splitlines()],
        )
        cover_letter_path.write_text(cover_letter_text, encoding="utf-8")

        return ApplicationPackage(
            resume_path=resume_path,
            cover_letter_path=cover_letter_path,
            resume_preview=resume_preview,
            cover_letter_text=cover_letter_text,
            ats_score=ats_score,
            matched_keywords=matched_keywords,
            added_keywords=added_keywords,
        )

    def _finalize_run(
        self,
        board: str,
        results: list[JobAutomationResult],
        excluded_results: list[JobAutomationResult],
        counts: "_RunCounts",
    ) -> AutopilotRun:
        run = AutopilotRun(
            board=board,
            results=results,
            excluded_results=excluded_results,
            total_scraped=counts.total_scraped,
            filtered_out_count=counts.filtered_out_count,
            excluded_external_count=counts.excluded_external_count,
            excluded_unconfirmed_count=counts.excluded_unconfirmed_count,
            auto_applied_count=counts.auto_applied_count,
            queued_count=counts.queued_count,
            failed_count=counts.failed_count,
            ats_blocked_count=counts.ats_blocked_count,
        )
        manifest_path = self._persist_run(run)
        return run.model_copy(update={"manifest_path": manifest_path})

    def _persist_run(self, run: AutopilotRun) -> Path:
        board_dir = self.artifact_dir / run.board
        board_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = board_dir / "latest-run.json"
        payload = run.model_dump(mode="json")
        payload["manifest_path"] = str(manifest_path)
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return manifest_path

    @staticmethod
    def _default_apply_message(handler_name: str, status: ApplicationStatus) -> str:
        if status == ApplicationStatus.APPLIED:
            return f"Application submitted through {handler_name}."
        if status == ApplicationStatus.SKIPPED:
            return f"{handler_name} marked this job for review."
        return f"{handler_name} could not complete the application."


class _RunCounts:
    def __init__(self) -> None:
        self.total_scraped = 0
        self.filtered_out_count = 0
        self.excluded_external_count = 0
        self.excluded_unconfirmed_count = 0
        self.auto_applied_count = 0
        self.queued_count = 0
        self.failed_count = 0
        self.ats_blocked_count = 0

    def capture(self, result: JobAutomationResult) -> None:
        if result.excluded_from_active_list:
            if result.exclusion_reason == "easy_apply_unconfirmed":
                self.excluded_unconfirmed_count += 1
            else:
                self.excluded_external_count += 1
            return
        if result.automation_state == AutomationState.APPLIED:
            self.auto_applied_count += 1
        elif result.automation_state == AutomationState.QUEUED:
            self.queued_count += 1
        elif result.automation_state == AutomationState.FAILED:
            self.failed_count += 1
        elif result.automation_state == AutomationState.NEEDS_REVIEW:
            self.ats_blocked_count += 1


def detect_application_route(job: Job) -> ApplicationRoute:
    url = (job.url or "").lower()
    if job.source_board == "linkedin":
        return ApplicationRoute.LINKEDIN_EASY_APPLY if job.easy_apply else ApplicationRoute.LINKEDIN_EXTERNAL
    if "greenhouse.io" in url or job.source_board == "greenhouse":
        return ApplicationRoute.GREENHOUSE
    if "myworkdayjobs.com" in url or "myworkdaysite.com" in url or job.source_board == "workday":
        return ApplicationRoute.WORKDAY
    if "lever.co" in url or job.source_board == "lever":
        return ApplicationRoute.LEVER
    return ApplicationRoute.GENERIC


def build_tailored_resume_preview(
    profile: UserProfile,
    job: Job,
    *,
    matched_keywords: list[str],
    added_keywords: list[str],
) -> str:
    target_keywords = ", ".join(unique_in_order([*matched_keywords, *added_keywords])[:8]) or "General fit"
    experience_lines = []
    for experience in profile.work_experience[:3]:
        summary = shorten_line(experience.description or experience.title, limit=140)
        experience_lines.append(f"- {experience.title} at {experience.company}: {summary}")

    return "\n".join(
        [
            f"{profile.first_name} {profile.last_name}",
            profile.headline or "",
            f"Target role: {job.title} at {job.company}",
            "",
            "Professional summary",
            profile.summary or f"{profile.years_of_experience or 'Several'} years of relevant experience.",
            "",
            "Top aligned keywords",
            target_keywords,
            "",
            "Relevant experience",
            *(experience_lines or ["- Experience details will be pulled from the user profile."]),
        ]
    ).strip()


def build_tailored_cover_letter(
    profile: UserProfile,
    job: Job,
    *,
    matched_keywords: list[str],
    added_keywords: list[str],
) -> str:
    highlighted = ", ".join(unique_in_order([*matched_keywords[:4], *added_keywords[:2]])) or "delivery and execution"
    summary_line = profile.summary or (
        f"I bring {profile.years_of_experience or 'several'} years of hands-on experience."
    )
    return (
        f"Dear Hiring Team,\n\n"
        f"I am applying for the {job.title} role at {job.company}. My background aligns strongly with "
        f"the core themes in this job, especially {highlighted}.\n\n"
        f"{summary_line} "
        f"I would welcome the chance to contribute that experience to your team.\n\n"
        f"Best regards,\n"
        f"{profile.first_name} {profile.last_name}"
    )


def write_simple_docx(path: Path, *, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = []
    for line in lines:
        text = escape_xml(line or " ")
        paragraphs.append(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            f"{text}"
            "</w:t></w:r></w:p>"
        )

    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(paragraphs)
        + (
            "<w:sectPr>"
            "<w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
            "<w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\" "
            "w:header=\"720\" w:footer=\"720\" w:gutter=\"0\"/>"
            "</w:sectPr>"
        )
        + "</w:body></w:document>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    relationships = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)


def ordered_terms(*texts: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw_token in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{1,}", text.lower()):
            token = raw_token.strip(".,:/-")
            if not token or token in STOPWORDS:
                continue
            if len(token) <= 2 and token not in {"ai", "ml", "pm"}:
                continue
            if token not in seen:
                seen.add(token)
                terms.append(token)
    return terms


def unique_in_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return ordered


def has_ai_concept(terms: Iterable[str]) -> bool:
    normalized = {term.strip().lower() for term in terms if term.strip()}
    if normalized & {"ai", "genai", "llm", "ml"}:
        return True
    if {"machine", "learning"}.issubset(normalized):
        return True
    if {"artificial", "intelligence"}.issubset(normalized):
        return True
    return False


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "application"


def shorten_line(text: str, *, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."
