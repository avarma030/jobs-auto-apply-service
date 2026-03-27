from __future__ import annotations

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import ApplicationPackage, Job


class LeverApplier(BaseApplier):
    """Applies to jobs hosted on Lever ATS (jobs.lever.co)."""

    board_name = "Lever"
    board_slug = "lever"

    def can_apply(self, job: Job) -> bool:
        return "lever.co" in (job.url or "") or job.source_board == self.board_slug

    async def apply(self, job: Job, package: ApplicationPackage | None = None) -> ApplicationResult:
        try:
            # TODO:
            # 1. Navigate to job.url (jobs.lever.co/...)
            # 2. Fill name, email, phone, links
            # 3. Upload resume
            # 4. Fill custom questions
            # 5. Submit
            raise NotImplementedError("Lever applier not yet implemented")
        except Exception as exc:
            return self._fail(job, str(exc))
