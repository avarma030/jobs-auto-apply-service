from __future__ import annotations

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import Job


class GreenhouseApplier(BaseApplier):
    """Applies to jobs hosted on Greenhouse ATS (boards.greenhouse.io).

    Greenhouse application forms are relatively uniform:
    - Personal info
    - Resume / cover letter upload
    - Custom questions (text, dropdowns, checkboxes)
    - Demographics (optional / voluntary)
    - Submit
    """

    board_name = "Greenhouse"
    board_slug = "greenhouse"

    def can_apply(self, job: Job) -> bool:
        return "greenhouse.io" in (job.url or "") or job.source_board == self.board_slug

    async def apply(self, job: Job) -> ApplicationResult:
        try:
            # TODO:
            # 1. Navigate to job.url (boards.greenhouse.io/...)
            # 2. Fill standard fields from self.profile
            # 3. Upload resume from self.profile.resume_path
            # 4. Answer custom questions using self.profile.custom_answers
            # 5. Submit
            raise NotImplementedError("Greenhouse applier not yet implemented")
        except Exception as exc:
            return self._fail(job, str(exc))
