from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

from src.models import ApplicationStatus, Job, UserProfile


@dataclass
class ApplicationQuestionPrompt:
    question: str
    field_type: str
    options: list[str] = field(default_factory=list)


@dataclass
class AnsweredQuestion:
    question: str
    answer: str
    source: str
    field_type: str


class ApplicationResult:
    def __init__(
        self,
        job: Job,
        status: ApplicationStatus,
        message: str = "",
        confirmation_id: str | None = None,
        new_questions: list[str] | None = None,
        answered_questions: list[AnsweredQuestion] | None = None,
        learned_answers: dict[str, str] | None = None,
    ):
        self.job = job
        self.status = status
        self.message = message
        self.confirmation_id = confirmation_id
        # Questions encountered during the application with no pre-set answer.
        # The orchestrator will use Claude to generate answers and save them back
        # to the profile so future applications answer them automatically.
        self.new_questions: list[str] = new_questions or []
        self.answered_questions: list[AnsweredQuestion] = answered_questions or []
        self.learned_answers: dict[str, str] = learned_answers or {}

    def __repr__(self) -> str:
        return (
            f"ApplicationResult(job={self.job.title!r}, "
            f"company={self.job.company!r}, status={self.status})"
        )


class BaseApplier(abc.ABC):
    """Abstract base class for job-board application automators.

    Each concrete applier handles the apply workflow for one job board,
    navigating forms, uploading resumes, and submitting applications.
    """

    board_name: str = ""
    board_slug: str = ""

    def __init__(self, profile: UserProfile):
        self.profile = profile
        self.progress_callback: Callable[[str], None] | None = None
        self.answer_resolver: Callable[
            [list[ApplicationQuestionPrompt]],
            Awaitable[dict[str, str]],
        ] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BaseApplier":
        await self.setup()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.teardown()

    async def setup(self) -> None:
        """Launch browser, log in, etc."""
        logger.debug(f"[{self.board_name}] Setting up applier")

    async def teardown(self) -> None:
        """Close browser, release resources."""
        logger.debug(f"[{self.board_name}] Tearing down applier")

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def apply(
        self,
        job: Job,
        tailored_resume_path: str | None = None,
        cover_letter: str | None = None,
    ) -> ApplicationResult:
        """Attempt to apply to *job* using *self.profile*.

        Args:
            job: The job to apply to.
            tailored_resume_path: Absolute path to the tailored resume PDF, if generated.
            cover_letter: Cover letter text, if generated.

        Returns an ``ApplicationResult`` regardless of success/failure.
        Implementations must never raise — catch all errors and return
        a FAILED result with a descriptive message.
        """
        ...

    def can_apply(self, job: Job) -> bool:
        """Return True if this applier supports the given job.

        Default implementation checks ``job.source_board``.
        """
        return job.source_board == self.board_slug

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ok(self, job: Job, confirmation_id: str | None = None) -> ApplicationResult:
        return ApplicationResult(job, ApplicationStatus.APPLIED, confirmation_id=confirmation_id)

    def _fail(self, job: Job, reason: str) -> ApplicationResult:
        logger.warning(f"[{self.board_name}] Failed to apply to {job.title} @ {job.company}: {reason}")
        return ApplicationResult(job, ApplicationStatus.FAILED, message=reason)

    def _skip(self, job: Job, reason: str) -> ApplicationResult:
        logger.info(f"[{self.board_name}] Skipped {job.title} @ {job.company}: {reason}")
        return ApplicationResult(job, ApplicationStatus.SKIPPED, message=reason)

    def _emit_progress(self, message: str) -> None:
        callback = getattr(self, "progress_callback", None)
        if callback:
            callback(message)
