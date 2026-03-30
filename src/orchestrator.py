from __future__ import annotations

import asyncio
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
from src.services.application_questions import normalize_question_text
from src.services.job_classifier import detect_ats
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

# Where tailored assets are stored: data/uploads/{user_id}/tailored/{job_id}/
_UPLOAD_BASE = Path("data/uploads")


class Orchestrator:
    """Coordinates scraping and applying across all enabled job boards."""

    def __init__(self, profile: UserProfile, db: Database):
        self.profile = profile
        self.db = db
        self._ai_client: anthropic.AsyncAnthropic | None = None

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
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Scrape task failed: {r}")
            else:
                total += r  # type: ignore[operator]
        logger.info(f"Scraping complete — {total} new jobs found")
        return total

    async def run_apply(
        self,
        user_id: int | None = None,
        scrape_run_id: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """Apply to pending jobs. Returns status counts."""
        def _emit(msg: str) -> None:
            if progress_callback:
                progress_callback(msg)
            else:
                logger.info(msg)

        pending = await self.db.get_pending_jobs(
            limit=settings.max_applications_per_run,
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
        for record in pending:
            job = self._record_to_job(record)
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
                continue

            status = result.status
            status_key = status.value if isinstance(status, ApplicationStatus) else str(status)
            counts[status_key] = counts.get(status_key, 0) + 1
            if status == ApplicationStatus.APPLIED:
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

        criteria_json = json.dumps(
            self._search_criteria_for_log(search_filter),
            ensure_ascii=False,
        )
        _emit(f"[Search][Criteria] {criteria_json}")
        _emit("🚀 Pipeline starting — scraping jobs …")
        jobs_found = await self.run_scrape(search_filter, user_id=user_id, run_id=run_id)
        _emit(f"✅ Scrape complete: {jobs_found} new jobs found")

        # If tailoring is disabled, apply the jobs from this run with the
        # uploaded resume as-is.
        if not tailor_documents:
            _emit("📎 Tailoring disabled for this run — applying scraped jobs with the uploaded resume.")
            counts = await self.run_apply(
                user_id=user_id,
                scrape_run_id=run_id,
                progress_callback=_emit,
            )
            counts["scraped"] = jobs_found
            return counts

        # Parse resume once — try per-user path first, then global fallback
        resume_text = ""
        resume_path = settings.resume_path
        if user_id is not None:
            user_resume = Path("data/uploads") / str(user_id) / "resume.pdf"
            if user_resume.exists():
                resume_path = user_resume
        if resume_path.exists():
            try:
                resume_text = resume_parser.parse_resume(resume_path)
                _emit(f"📄 Resume parsed ({len(resume_text):,} chars)")
            except Exception as exc:
                logger.error(f"Resume parse error: {exc}")

        ai = self._get_ai_client()

        # Load ONLY jobs from this run — never re-process leftover pending jobs from
        # previous runs, which would give wrong counts and re-score already-seen jobs.
        pending = await self.db.get_pending_jobs(
            limit=settings.max_applications_per_run,
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

            score = await ai_matcher.score_compatibility(
                resume_text, job.title, job.description or "", ai, settings.anthropic_model
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
            uid = user_id or 0
            job_dir = _UPLOAD_BASE / str(uid) / "tailored" / str(record.id)

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
                    tailored_text, ats_score = await resume_tailor.tailor_resume(
                        resume_text,
                        job.title,
                        job.description,
                        ai,
                        settings.anthropic_model,
                        target_ats_score=settings.min_ats_score,
                        max_attempts=settings.max_tailor_attempts,
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
                        self.profile, job, tailored_text, ai, settings.anthropic_model
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

    def _configure_applier(
        self,
        applier: BaseApplier,
        user_id: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
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
        del user_id

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

        try:
            save_profile(self.profile, settings.user_profile_path)
        except Exception as exc:
            logger.warning(f"Could not save profile to file: {exc}")

        if user_id is not None:
            try:
                await self.db.update_profile_custom_answers(user_id, changed_answers)
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
            async with scraper_cls(credentials=creds) as scraper:
                async for job in scraper.search(search_filter):
                    # Fetch full details (description, salary, skills) while
                    # the scraper session is still warm and has active cookies
                    try:
                        job = await scraper.get_job_details(job)
                    except Exception as exc:
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
                            f"authenticated LinkedIn session - keeping candidate for live "
                            f"apply verification: {job.title}"
                        )
                    await self.db.upsert_job(job, user_id=user_id, scrape_run_id=run_id)
                    count += 1
                    if search_filter.max_jobs is not None and count >= search_filter.max_jobs:
                        logger.info(
                            f"[{board}] Reached max_jobs limit ({search_filter.max_jobs}) "
                            "after detail verification"
                        )
                        break
                    await asyncio.sleep(settings.request_delay_seconds)
        except NotImplementedError:
            logger.warning(f"[{board}] Scraper not yet implemented — skipping")
        except Exception as exc:
            logger.error(f"[{board}] Scraper error: {exc}")
        return count

    def _pick_applier(self, job: Job) -> BaseApplier:
        for cls in APPLIER_REGISTRY:
            instance = cls(profile=self.profile)
            if instance.can_apply(job):
                return instance
        return GenericApplier(profile=self.profile)

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
