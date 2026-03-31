from __future__ import annotations

import asyncio
import inspect as pyinspect
import json
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import Callable, Type

import anthropic
from loguru import logger

from src.appliers.base import ApplicationQuestionPrompt, BaseApplier
from src.appliers.generic import GenericApplier
from src.appliers.greenhouse import GreenhouseApplier
from src.appliers.lever import LeverApplier
from src.appliers.linkedin import LinkedInApplier
from src.appliers.workday import WorkdayApplier
from src.config import settings
from src.database import Database
from src.models import ApplicationStatus, Job, JobSearchFilter, UserProfile
from src.scrapers.base import BaseScraper
from src.scrapers.dice import DiceScraper
from src.scrapers.glassdoor import GlassdoorScraper
from src.scrapers.greenhouse import GreenhouseScraper
from src.scrapers.indeed import IndeedScraper
from src.scrapers.lever import LeverScraper
from src.scrapers.linkedin import LinkedInScraper
from src.scrapers.monster import MonsterScraper
from src.scrapers.workday import WorkdayScraper
from src.scrapers.ziprecruiter import ZipRecruiterScraper
from src.services import ai_matcher, cover_letter as cover_letter_svc, pdf_builder, profile_extractor, resume_parser, resume_tailor
from src.services import ai_knowledge
from src.services.application_questions import normalize_question_text
from src.services.job_classifier import detect_ats
from src.services.runtime_state import user_resume_path, user_tailored_dir
from src.utils.profile_loader import save_profile

SCRAPER_REGISTRY: dict[str, Type[BaseScraper]] = {
    "linkedin": LinkedInScraper,
    "indeed": IndeedScraper,
    "glassdoor": GlassdoorScraper,
    "ziprecruiter": ZipRecruiterScraper,
    "dice": DiceScraper,
    "monster": MonsterScraper,
    "lever": LeverScraper,
    "greenhouse": GreenhouseScraper,
    "workday": WorkdayScraper,
}

APPLIER_REGISTRY: list[Type[BaseApplier]] = [
    LinkedInApplier,
    GreenhouseApplier,
    LeverApplier,
    WorkdayApplier,
    GenericApplier,  # fallback — must be last
]

class Orchestrator:
    """Coordinates scraping and applying across all enabled job boards."""

    def __init__(
        self,
        profile: UserProfile,
        db: Database,
        *,
        runtime_scope: str = "web",
    ):
        self.profile = profile
        self.db = db
        self.runtime_scope = runtime_scope
        self._ai_client: anthropic.AsyncAnthropic | None = None
        self._resume_text_cache: str = ""
        self._candidate_knowledge_pack: dict | None = None
        self._job_knowledge_pack_cache: dict[int, dict] = {}

    @staticmethod
    def _search_criteria_for_log(search_filter: JobSearchFilter) -> dict[str, object]:
        raw = search_filter.model_dump(mode="json", exclude_none=True)
        if raw.get("max_age_hours") is not None:
            raw.pop("max_age_days", None)
        return {
            key: value
            for key, value in raw.items()
            if value not in ("", [], {})
        }

    @staticmethod
    def _resolve_current_run_limit(
        search_filter: JobSearchFilter,
        jobs_found: int,
    ) -> int:
        if search_filter.max_jobs is not None:
            return search_filter.max_jobs
        return max(jobs_found, 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_scrape(
        self,
        search_filter: JobSearchFilter,
        user_id: int | None = None,
        run_id: str | None = None,
    ) -> int:
        """Scrape all enabled boards and persist discovered jobs. Returns count."""
        boards = settings.enabled_board_list()
        logger.info(f"Scraping {len(boards)} board(s): {boards}")

        total = 0
        tasks = [
            self._scrape_board(board, search_filter, user_id=user_id, run_id=run_id)
            for board in boards
            if board in SCRAPER_REGISTRY
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        auth_failure: Exception | None = None
        for r in results:
            if isinstance(r, Exception):
                if self._is_fatal_linkedin_auth_error(r):
                    auth_failure = r
                logger.error(f"Scrape task failed: {r}")
            else:
                total += r  # type: ignore[operator]
        if auth_failure is not None:
            raise auth_failure
        logger.info(f"Scraping complete — {total} new jobs found")
        return total

    async def run_apply(
        self,
        user_id: int | None = None,
        scrape_run_id: str | None = None,
        limit: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> dict:
        """Apply to pending jobs. Returns status counts."""
        def _emit(msg: str) -> None:
            if progress_callback:
                progress_callback(msg)
            else:
                logger.info(msg)

        async def _cancel_requested() -> bool:
            if should_cancel is None:
                return False
            decision = should_cancel()
            if pyinspect.isawaitable(decision):
                return bool(await decision)
            return bool(decision)

        pending_limit = limit if limit is not None else settings.max_applications_per_run
        pending = await self.db.get_pending_jobs(
            limit=pending_limit,
            user_id=user_id,
            scrape_run_id=scrape_run_id,
        )
        _emit(f"Applying to {len(pending)} pending jobs")

        counts: dict[str, int] = {
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "dry_run": 0,
        }
        if await _cancel_requested():
            _emit("Run cancellation requested — stopping before application phase.")
            counts["cancelled"] = 1
            return counts
        blocked_boards: dict[str, str] = {}
        for record in pending:
            if await _cancel_requested():
                _emit("Run cancellation requested — stopping application phase.")
                counts["cancelled"] = 1
                break
            job = self._record_to_job(record)
            board_block_reason = blocked_boards.get(job.source_board)
            if board_block_reason:
                counts["failed"] += 1
                _emit(
                    f"  ⚠️  Application result for '{job.title}': failed — {board_block_reason}"
                )
                await self.db.update_job_status(
                    record.id,
                    ApplicationStatus.FAILED,
                    notes=board_block_reason,
                )
                await self.db.log_application(
                    record.id,
                    ApplicationStatus.FAILED,
                    message=board_block_reason,
                    user_id=user_id,
                )
                continue
            cover_letter_text: str | None = None
            if getattr(record, "cover_letter_path", None):
                cl_path = Path(record.cover_letter_path)
                if cl_path.exists():
                    try:
                        cover_letter_text = cl_path.read_text(encoding="utf-8")
                    except Exception as exc:
                        logger.warning(f"Could not read cover letter for job {record.id}: {exc}")

            if settings.dry_run:
                _emit(f"🧪 [DRY RUN] Would apply to '{job.title}' @ {job.company}")
                counts["dry_run"] += 1
                continue

            applier = self._pick_applier(job)
            self._configure_applier(applier, user_id=user_id, progress_callback=_emit)
            _emit(f"📤 Applying to '{job.title}' @ {job.company} …")
            try:
                async with applier as a:
                    result = await a.apply(
                        job,
                        tailored_resume_path=record.tailored_resume_path,
                        cover_letter=cover_letter_text,
                    )
            except Exception as exc:
                logger.error(f"Applier error for job {record.id}: {exc}")
                counts["failed"] += 1
                await self.db.update_job_status(record.id, ApplicationStatus.FAILED, notes=str(exc))
                if self._should_halt_board_after_failure(job.source_board, str(exc)):
                    blocked_boards[job.source_board] = str(exc)
                    _emit(
                        f"[{job.source_board}] Halting remaining applications for this board: {exc}"
                    )
                continue

            status = result.status
            status_key = status.value if isinstance(status, ApplicationStatus) else str(status)
            counts[status_key] = counts.get(status_key, 0) + 1
            if status == ApplicationStatus.APPLIED:
                if result.message:
                    _emit(
                        f"  🎉 Application result for '{job.title}': applied — {result.message}"
                    )
                else:
                    _emit(f"  🎉 Applied successfully to '{job.title}' @ {job.company}!")
            else:
                _emit(f"  ⚠️  Application result for '{job.title}': {status_key} — {result.message or ''}")
            await self.db.update_job_status(
                record.id,
                status,
                applied_at=datetime.utcnow() if status == ApplicationStatus.APPLIED else None,
                notes=result.message,
            )
            await self.db.log_application(
                record.id,
                status,
                confirmation_id=result.confirmation_id,
                message=result.message,
                user_id=user_id,
            )
            if status == ApplicationStatus.FAILED and self._should_halt_board_after_failure(
                job.source_board, result.message
            ):
                blocked_boards[job.source_board] = result.message or "Authentication unavailable"
                _emit(
                    f"[{job.source_board}] Halting remaining applications for this board: "
                    f"{blocked_boards[job.source_board]}"
                )
            if result.learned_answers:
                saved_count = await self._persist_custom_answers(
                    result.learned_answers,
                    user_id=user_id,
                    progress_callback=_emit,
                )
                if saved_count:
                    _emit(
                        f"[Profile][Saved] Stored {saved_count} new question-answer pair(s) for future applications"
                    )
            new_question_prompts = list(getattr(result, "new_question_prompts", []) or [])
            if not new_question_prompts and result.new_questions:
                new_question_prompts = [
                    ApplicationQuestionPrompt(question=question, field_type="text")
                    for question in result.new_questions
                    if question.strip()
                ]
            if new_question_prompts:
                _emit(
                    f"  💡 Learned {len(new_question_prompts)} new Q&A pair(s) from this application"
                )
                await self._handle_new_questions(
                    new_question_prompts,
                    user_id=user_id,
                    progress_callback=_emit,
                )

        _emit(
            f"🏁 Apply run complete — applied: {counts['applied']}, "
            f"dry_run: {counts.get('dry_run', 0)}, "
            f"failed: {counts['failed']}, "
            f"skipped: {counts['skipped']}"
        )
        return counts

    async def run_full_pipeline(
        self,
        search_filter: JobSearchFilter,
        user_id: int | None = None,
        run_id: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
        tailor_documents: bool = True,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> dict:
        """
        End-to-end pipeline:
          1. Scrape jobs from all enabled boards
          2. Score each job against the user's resume (skip < min_match_score)
          3. Detect ATS type
          4. Tailor resume for each qualified job (loop until ATS score ≥ min_ats_score)
          5. Generate tailored cover letter
          6. Apply using the appropriate applier
        Returns aggregate counts.
        """
        def _ts() -> str:
            """Current time as [HH:MM:SS] prefix."""
            return datetime.utcnow().strftime("[%H:%M:%S]")

        def _emit(msg: str) -> None:
            logger.info(msg)
            if progress_callback:
                progress_callback(f"{_ts()} {msg}")

        async def _cancel_requested() -> bool:
            if should_cancel is None:
                return False
            decision = should_cancel()
            if pyinspect.isawaitable(decision):
                return bool(await decision)
            return bool(decision)

        criteria_json = json.dumps(
            self._search_criteria_for_log(search_filter),
            ensure_ascii=False,
        )
        _emit(f"[Search][Criteria] {criteria_json}")
        _emit("🚀 Pipeline starting — scraping jobs …")
        if await _cancel_requested():
            _emit("Run cancellation requested — stopping before scrape phase.")
            return {"applied": 0, "failed": 0, "skipped": 0, "dry_run": 0, "scraped": 0, "cancelled": 1}
        jobs_found = await self.run_scrape(search_filter, user_id=user_id, run_id=run_id)
        current_run_limit = self._resolve_current_run_limit(search_filter, jobs_found)
        _emit(f"✅ Scrape complete: {jobs_found} new jobs found")

        if search_filter.max_jobs is not None and jobs_found < search_filter.max_jobs:
            _emit(
                f"[Search][Result] Requested {search_filter.max_jobs} jobs, "
                f"but only {jobs_found} matched the current criteria. Proceeding with {jobs_found}."
            )

        # If tailoring is disabled, apply the jobs from this run with the
        # uploaded resume as-is.
        if not tailor_documents:
            _emit("📎 Tailoring disabled for this run — applying scraped jobs with the uploaded resume.")
            counts = await self.run_apply(
                user_id=user_id,
                scrape_run_id=run_id,
                limit=current_run_limit,
                progress_callback=_emit,
                should_cancel=_cancel_requested,
            )
            counts["scraped"] = jobs_found
            return counts

        # Parse resume once — try per-user path first, then global fallback
        resume_text = ""
        resume_path: Path | None = None
        if self.profile.resume_path:
            resume_path = Path(self.profile.resume_path)
        elif user_id is not None:
            candidate_resume = user_resume_path(user_id)
            if candidate_resume.exists():
                resume_path = candidate_resume
        elif self.runtime_scope == "cli" and settings.resume_path:
            resume_path = Path(settings.resume_path)
        if resume_path and resume_path.exists():
            try:
                resume_text = resume_parser.parse_resume(resume_path)
                self._resume_text_cache = resume_text
                _emit(f"📄 Resume parsed ({len(resume_text):,} chars)")
            except Exception as exc:
                logger.error(f"Resume parse error: {exc}")
                self._resume_text_cache = ""
        else:
            self._resume_text_cache = ""

        ai = self._get_ai_client()
        if ai and resume_text and user_id is not None:
            try:
                await self._ensure_candidate_knowledge_pack(user_id=user_id, resume_text=resume_text)
            except Exception as exc:
                logger.warning(f"Could not build candidate knowledge pack: {exc}")

        # Load ONLY jobs from this run — never re-process leftover pending jobs from
        # previous runs, which would give wrong counts and re-score already-seen jobs.
        pending = await self.db.get_pending_jobs(
            limit=current_run_limit,
            user_id=user_id,
            scrape_run_id=run_id,
        )

        # Note: get_job_details() is called inline per-job in _scrape_board() while the
        # scraper session is warm. Jobs that still have no description are skipped during
        # scoring below with a clear message — no separate second-pass needed.

        if not resume_text:
            _emit(
                "⚠️  No resume found — scoring and tailoring disabled. "
                "Upload a resume via the Profile page to enable AI features."
            )
        if not ai:
            _emit("⚠️  ANTHROPIC_API_KEY not set — scoring and tailoring disabled.")

        # Resolve match threshold: per-run override takes precedence over global setting
        effective_threshold = (
            search_filter.min_match_score
            if search_filter.min_match_score is not None
            else settings.min_match_score
        )
        _emit(f"🧠 Scoring {len(pending)} jobs — threshold: {effective_threshold:.0f}% …")

        qualified: list[tuple] = []  # (record, job)
        skipped_score = 0

        for i, record in enumerate(pending, 1):
            if await _cancel_requested():
                _emit("Run cancellation requested — stopping during scoring.")
                return {
                    "applied": 0,
                    "failed": 0,
                    "skipped": skipped_score,
                    "dry_run": 0,
                    "scraped": jobs_found,
                    "cancelled": 1,
                }
            job = self._record_to_job(record)
            if not resume_text or not ai:
                # Cannot score — leave as pending for manual review, do not auto-qualify
                continue
            if not job.description:
                _emit(f"  ⏭️  [{i}/{len(pending)}] '{job.title}' @ {job.company} — no description, skipped")
                skipped_score += 1
                await self.db.update_job_status(
                    record.id, ApplicationStatus.SKIPPED,
                    notes="No job description available for AI scoring",
                )
                continue
            try:
                await self._ensure_job_knowledge_pack(
                    record_id=record.id,
                    job=job,
                    user_id=user_id,
                )
            except Exception as exc:
                logger.warning(f"Could not build job knowledge pack for job {record.id}: {exc}")

            score = await ai_matcher.score_compatibility(
                resume_text,
                job.title,
                job.description or "",
                ai,
                settings.anthropic_model,
                search_keywords=search_filter.keywords,
                profile=self.profile,
                cache_backend=self.db,
                user_id=user_id,
                job_id=record.id,
            )
            job.match_score = score

            # Persist match score
            await self.db.update_job_ai_fields(record.id, match_score=score)

            if score < effective_threshold:
                skipped_score += 1
                _emit(f"  ❌ [{i}/{len(pending)}] '{job.title}' @ {job.company} — {score:.0f}% (below {effective_threshold:.0f}%)")
                await self.db.update_job_status(
                    record.id,
                    ApplicationStatus.SKIPPED,
                    notes=f"Match score {score:.0f}% < threshold {effective_threshold:.0f}%",
                )
            else:
                _emit(f"  ✅ [{i}/{len(pending)}] '{job.title}' @ {job.company} — {score:.0f}% match ✓")
                qualified.append((record, job))

        _emit(f"📊 Scoring done — {len(qualified)} qualified, {skipped_score} skipped")

        # ------------------------------------------------------------------
        # For each qualified job: ATS detect → tailor → cover letter → apply
        # ------------------------------------------------------------------
        counts: dict[str, int] = {
            "applied": 0,
            "failed": 0,
            "skipped": skipped_score,  # already-skipped-by-score count
            "dry_run": 0,
            "scraped": jobs_found,     # total scraped — for run record
        }

        for idx, (record, job) in enumerate(qualified, 1):
            if await _cancel_requested():
                _emit("Run cancellation requested — stopping before remaining tailoring/apply steps.")
                counts["cancelled"] = 1
                break
            uid = user_id or 0
            job_dir = user_tailored_dir(uid, record.id)

            tailored_resume_path: str | None = None
            cl_text: str | None = None
            ats_score: float | None = None

            # Detect ATS type
            ats_type = detect_ats(job)
            job.ats_type = ats_type

            if resume_text and job.description and ai:
                # Tailor resume
                _emit(f"✏️  [{idx}/{len(qualified)}] Tailoring resume for '{job.title}' @ {job.company} …")
                try:
                    candidate_pack = self._candidate_knowledge_pack
                    job_pack = await self._ensure_job_knowledge_pack(
                        record_id=record.id,
                        job=job,
                        user_id=user_id,
                    )
                    tailored_text, ats_score = await resume_tailor.tailor_resume(
                        resume_text,
                        job.title,
                        job.description,
                        ai,
                        settings.anthropic_model,
                        target_ats_score=settings.min_ats_score,
                        max_attempts=settings.max_tailor_attempts,
                        candidate_pack=candidate_pack,
                        job_pack=job_pack,
                        cache_backend=self.db,
                        user_id=user_id,
                    )
                    _emit(f"     📈 ATS score after tailoring: {ats_score:.0f}%")

                    # Build tailored PDF
                    pdf_path = job_dir / "resume.pdf"
                    pdf_builder.build_resume_pdf(tailored_text, pdf_path)
                    tailored_resume_path = str(pdf_path)
                    job.tailored_resume_path = tailored_resume_path

                    # Generate cover letter
                    _emit(f"     📝 Generating cover letter …")
                    cl_text = await cover_letter_svc.generate_cover_letter(
                        self.profile,
                        job,
                        tailored_text,
                        ai,
                        settings.anthropic_model,
                        candidate_pack=candidate_pack,
                        job_pack=job_pack,
                        cache_backend=self.db,
                        user_id=user_id,
                    )
                    cl_path = job_dir / "cover_letter.md"
                    cl_path.parent.mkdir(parents=True, exist_ok=True)
                    cl_path.write_text(cl_text)
                    job.cover_letter_path = str(cl_path)

                    await self.db.update_job_ai_fields(
                        record.id,
                        ats_score=ats_score,
                        ats_type=ats_type,
                        tailored_resume_path=tailored_resume_path,
                        cover_letter_path=str(cl_path),
                    )
                except Exception as exc:
                    logger.error(f"AI tailoring error for job {record.id}: {exc}")
                    await self.db.update_job_status(
                        record.id, ApplicationStatus.FAILED,
                        notes=f"Resume tailoring error: {str(exc)[:200]}",
                    )
                    counts["failed"] += 1
                    continue

            if settings.dry_run:
                _emit(f"🧪 [DRY RUN] Would apply to '{job.title}' @ {job.company} (ATS: {ats_type})")
                counts["dry_run"] += 1
                continue

            _emit(f"📤 Applying to '{job.title}' @ {job.company} (ATS: {ats_type}) …")
            applier = self._pick_applier(job)
            self._configure_applier(applier, user_id=user_id, progress_callback=_emit)
            try:
                async with applier as a:
                    result = await a.apply(
                        job,
                        tailored_resume_path=tailored_resume_path,
                        cover_letter=cl_text,
                    )
            except Exception as exc:
                logger.error(f"Applier error for job {record.id}: {exc}")
                counts["failed"] += 1
                await self.db.update_job_status(record.id, ApplicationStatus.FAILED, notes=str(exc))
                continue

            status = result.status
            status_key = status.value if isinstance(status, ApplicationStatus) else str(status)
            counts[status_key] = counts.get(status_key, 0) + 1
            if status == ApplicationStatus.APPLIED:
                if result.message:
                    _emit(
                        f"  🎉 Application result for '{job.title}': applied — {result.message}"
                    )
                else:
                    _emit(f"  🎉 Applied successfully to '{job.title}' @ {job.company}!")
            else:
                _emit(f"  ⚠️  Application result for '{job.title}': {status_key} — {result.message or ''}")
            await self.db.update_job_status(
                record.id,
                status,
                applied_at=datetime.utcnow() if status == ApplicationStatus.APPLIED else None,
                notes=result.message,
            )
            await self.db.log_application(
                record.id,
                status,
                confirmation_id=result.confirmation_id,
                message=result.message,
                user_id=user_id,
            )
            if result.learned_answers:
                saved_count = await self._persist_custom_answers(
                    result.learned_answers,
                    user_id=user_id,
                    progress_callback=_emit,
                )
                if saved_count:
                    _emit(
                        f"[Profile][Saved] Stored {saved_count} new question-answer pair(s) for future applications"
                    )
            new_question_prompts = list(getattr(result, "new_question_prompts", []) or [])
            if not new_question_prompts and result.new_questions:
                new_question_prompts = [
                    ApplicationQuestionPrompt(question=question, field_type="text")
                    for question in result.new_questions
                    if question.strip()
                ]
            if new_question_prompts:
                _emit(
                    f"  💡 Learned {len(new_question_prompts)} new Q&A pair(s) from this application"
                )
                await self._handle_new_questions(
                    new_question_prompts,
                    user_id=user_id,
                    progress_callback=_emit,
                )

        _emit(
            f"🏁 Pipeline complete — applied: {counts['applied']}, "
            f"dry_run: {counts.get('dry_run', 0)}, "
            f"failed: {counts['failed']}, "
            f"skipped: {counts['skipped']}"
        )
        return counts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_ai_client(self) -> anthropic.AsyncAnthropic | None:
        if not settings.anthropic_api_key:
            logger.warning("ANTHROPIC_API_KEY not set — AI features disabled")
            return None
        if self._ai_client is None:
            self._ai_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        return self._ai_client

    async def _ensure_candidate_knowledge_pack(
        self,
        *,
        user_id: int | None,
        resume_text: str,
    ) -> dict | None:
        if not resume_text.strip() or user_id is None:
            return self._candidate_knowledge_pack
        pack = await ai_knowledge.ensure_candidate_knowledge_pack(
            self.db,
            user_id=user_id,
            profile=self.profile,
            resume_text=resume_text,
        )
        self._candidate_knowledge_pack = pack.model_dump(mode="json")
        return self._candidate_knowledge_pack

    async def _ensure_job_knowledge_pack(
        self,
        *,
        record_id: int,
        job: Job,
        user_id: int | None,
    ) -> dict | None:
        if not job.description:
            return None
        cached = self._job_knowledge_pack_cache.get(record_id)
        if cached is not None:
            return cached
        pack = await ai_knowledge.ensure_job_knowledge_pack(
            self.db,
            job_id=record_id,
            user_id=user_id,
            job=job,
        )
        payload = pack.model_dump(mode="json")
        self._job_knowledge_pack_cache[record_id] = payload
        return payload

    def _configure_applier(
        self,
        applier: BaseApplier,
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if hasattr(applier, "db"):
            applier.db = self.db
        if hasattr(applier, "user_id"):
            applier.user_id = user_id
        if hasattr(applier, "runtime_scope"):
            applier.runtime_scope = self.runtime_scope
        applier.progress_callback = progress_callback
        applier.answer_resolver = self._build_answer_resolver(
            user_id=user_id,
            progress_callback=progress_callback,
        )

    def _build_answer_resolver(
        self,
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> Callable[[list[ApplicationQuestionPrompt]], Awaitable[dict[str, str]]]:
        async def _resolve(prompts: list[ApplicationQuestionPrompt]) -> dict[str, str]:
            return await self._suggest_question_answers(
                prompts,
                user_id=user_id,
                progress_callback=progress_callback,
            )

        return _resolve

    async def _suggest_question_answers(
        self,
        prompts: list[ApplicationQuestionPrompt],
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        questions = [prompt.question.strip() for prompt in prompts if prompt.question.strip()]
        if not questions:
            return {}

        ai = self._get_ai_client()
        if not ai:
            logger.warning(f"New questions found but no AI client - skipping: {questions}")
            return {}

        if progress_callback and len(questions) > 1:
            progress_callback(
                f"[AI][Questions] Attempting to answer {len(questions)} new application question(s)"
            )

        try:
            return await profile_extractor.suggest_answers(
                prompts,
                self.profile,
                ai,
                settings.anthropic_model,
                cache_backend=self.db,
                user_id=user_id,
                resume_text=self._resume_text_cache,
            )
        except Exception as exc:
            logger.error(f"suggest_answers error: {exc}")
            return {}

    async def _persist_custom_answers(
        self,
        answers: dict[str, str],
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> int:
        cleaned_answers = {
            normalize_question_text(str(question)): str(answer).strip()
            for question, answer in answers.items()
            if normalize_question_text(str(question)) and str(answer).strip()
        }
        if not cleaned_answers:
            return 0

        changed_answers = {
            question: answer
            for question, answer in cleaned_answers.items()
            if self.profile.custom_answers.get(question) != answer
        }
        if not changed_answers:
            return 0

        self.profile.custom_answers.update(changed_answers)
        logger.info(
            f"Learned {len(changed_answers)} new Q&A pairs: {list(changed_answers.keys())}"
        )

        if self.runtime_scope == "cli":
            try:
                save_profile(self.profile, settings.user_profile_path)
            except Exception as exc:
                logger.warning(f"Could not save profile to file: {exc}")

        if user_id is not None:
            try:
                await self.db.update_profile_custom_answers(user_id, changed_answers)
                for question, answer in changed_answers.items():
                    await ai_knowledge.store_answer_memory(
                        self.db,
                        user_id=user_id,
                        question_text=question,
                        answer_text=answer,
                        source_kind="learned",
                        confidence=1.0,
                        approved=True,
                        evidence={"source": "application"},
                    )
            except Exception as exc:
                logger.warning(f"Could not save custom_answers to DB: {exc}")

        return len(changed_answers)

    async def _handle_new_questions(
        self,
        prompts: list[ApplicationQuestionPrompt],
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        prompts = [
            ApplicationQuestionPrompt(
                question=normalize_question_text(prompt.question),
                field_type=prompt.field_type,
                options=[str(option).strip() for option in prompt.options if str(option).strip()],
            )
            for prompt in prompts
            if normalize_question_text(prompt.question)
        ]
        new_answers = await self._suggest_question_answers(
            prompts,
            user_id=user_id,
            progress_callback=progress_callback,
        )
        if not new_answers:
            return

        await self._persist_custom_answers(
            new_answers,
            user_id=user_id,
            progress_callback=progress_callback,
        )

    async def _scrape_board(
        self,
        board: str,
        search_filter: JobSearchFilter,
        user_id: int | None = None,
        run_id: str | None = None,
    ) -> int:
        scraper_cls = SCRAPER_REGISTRY[board]
        creds = self._get_creds(board)
        count = 0
        try:
            try:
                scraper = scraper_cls(
                    credentials=creds,
                    db=self.db,
                    user_id=user_id,
                    runtime_scope=self.runtime_scope,
                )
            except TypeError:
                scraper = scraper_cls(credentials=creds)
            async with scraper as scraper:
                async for job in scraper.search(search_filter):
                    # Fetch full details (description, salary, skills) while
                    # the scraper session is still warm and has active cookies
                    try:
                        job = await scraper.get_job_details(job)
                    except Exception as exc:
                        if self._is_fatal_linkedin_auth_error(exc):
                            raise
                        logger.warning(f"[{board}] Detail fetch failed for {job.title}: {exc}")
                    # After detail fetch the easy_apply flag may have been updated;
                    # drop the job if it doesn't meet the easy-apply-only filter.
                    if search_filter.easy_apply_only and not job.easy_apply:
                        can_strictly_verify_easy_apply = True
                        if board == "linkedin":
                            has_authenticated_session = getattr(
                                scraper, "has_authenticated_session", None
                            )
                            if callable(has_authenticated_session):
                                can_strictly_verify_easy_apply = bool(
                                    has_authenticated_session()
                                )
                        if can_strictly_verify_easy_apply:
                            logger.debug(
                                f"[{board}] Skipping non-easy-apply job after detail fetch: {job.title}"
                            )
                            continue
                        logger.warning(
                            f"[{board}] Could not strictly verify Easy Apply without an "
                            f"authenticated LinkedIn session - aborting LinkedIn scrape "
                            f"because Easy Apply cannot be verified accurately: {job.title}"
                        )
                        raise RuntimeError(
                            "LinkedIn authentication unavailable: could not verify Easy Apply "
                            "accurately for this run."
                        )
                    await self.db.upsert_job(job, user_id=user_id, scrape_run_id=run_id)
                    count += 1
                    if search_filter.max_jobs is not None and count >= search_filter.max_jobs:
                        logger.info(
                            f"[{board}] Reached max_jobs limit ({search_filter.max_jobs}) "
                            "after detail verification"
                        )
                        break
                    delay_seconds = 0.0 if board == "linkedin" else settings.request_delay_seconds
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
        except NotImplementedError:
            logger.warning(f"[{board}] Scraper not yet implemented — skipping")
        except Exception as exc:
            if self._is_fatal_linkedin_auth_error(exc):
                raise
            logger.error(f"[{board}] Scraper error: {exc}")
        return count

    def _pick_applier(self, job: Job) -> BaseApplier:
        credentials = self._get_creds(job.source_board)
        for cls in APPLIER_REGISTRY:
            instance = cls(
                profile=self.profile,
                credentials=credentials,
                db=self.db,
                runtime_scope=self.runtime_scope,
            )
            if instance.can_apply(job):
                return instance
        return GenericApplier(
            profile=self.profile,
            credentials=credentials,
            db=self.db,
            runtime_scope=self.runtime_scope,
        )

    @staticmethod
    def _should_halt_board_after_failure(board: str, message: str | None) -> bool:
        text = (message or "").lower()
        return board == "linkedin" and text.startswith("linkedin authentication unavailable")

    @staticmethod
    def _is_fatal_linkedin_auth_error(exc: Exception) -> bool:
        return str(exc).lower().startswith("linkedin authentication unavailable")

    def _get_creds(self, board: str) -> dict:
        accounts = self.profile.job_board_accounts
        cred_obj = getattr(accounts, board, None)
        if cred_obj is None:
            return {}
        return cred_obj.model_dump(exclude_none=True)

    @staticmethod
    def _record_to_job(record) -> Job:  # type: ignore[no-untyped-def]
        import json as _json

        return Job(
            id=str(record.id),
            title=record.title,
            company=record.company,
            location=record.location,
            description=record.description,
            url=record.url,
            source_board=record.source_board,
            external_id=record.external_id,
            job_type=record.job_type,
            work_mode=record.work_mode,
            experience_level=record.experience_level,
            salary_min=record.salary_min,
            salary_max=record.salary_max,
            salary_currency=record.salary_currency,
            skills=_json.loads(record.skills) if record.skills else [],
            easy_apply=record.easy_apply,
            posted_at=record.posted_at,
            scraped_at=record.scraped_at,
            application_status=record.application_status,
            applied_at=record.applied_at,
            notes=record.notes,
            match_score=getattr(record, "match_score", None),
            ats_score=getattr(record, "ats_score", None),
            ats_type=getattr(record, "ats_type", None),
            tailored_resume_path=getattr(record, "tailored_resume_path", None),
            cover_letter_path=getattr(record, "cover_letter_path", None),
        )
