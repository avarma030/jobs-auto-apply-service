from __future__ import annotations

import abc

from loguru import logger

from src.models import ApplicationPackage, ApplicationStatus, Job, UserProfile


class ApplicationResult:
    def __init__(
        self,
        job: Job,
        status: ApplicationStatus,
        message: str = "",
        confirmation_id: str | None = None,
    ):
        self.job = job
        self.status = status
        self.message = message
        self.confirmation_id = confirmation_id

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
    async def apply(self, job: Job, package: ApplicationPackage | None = None) -> ApplicationResult:
        """Attempt to apply to *job* using *self.profile*.

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
