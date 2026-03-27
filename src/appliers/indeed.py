from __future__ import annotations

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import Job


class IndeedApplier(BaseApplier):
    """Applies to jobs on Indeed using Indeed's Instant Apply flow."""

    board_name = "Indeed"
    board_slug = "indeed"

    async def apply(self, job: Job) -> ApplicationResult:
        if not self.can_apply(job):
            return self._skip(job, "Not an Indeed job")
        try:
            # TODO:
            # 1. Navigate to job.url
            # 2. Click "Apply Now" / "Instant Apply"
            # 3. Fill resume, cover letter, screening questions
            # 4. Submit
            raise NotImplementedError("Indeed applier not yet implemented")
        except Exception as exc:
            return self._fail(job, str(exc))
