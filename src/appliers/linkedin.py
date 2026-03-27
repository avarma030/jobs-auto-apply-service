from __future__ import annotations

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import Job, UserProfile


class LinkedInApplier(BaseApplier):
    """Applies to jobs on LinkedIn using Easy Apply and external apply flows.

    Easy Apply:
    - Detect the "Easy Apply" button on a job listing page.
    - Step through the multi-page Playwright-driven modal:
      contact info → resume upload → screening questions → review → submit.

    External Apply:
    - Click "Apply" which redirects to the company's ATS.
    - Delegate to the appropriate ATS applier (Greenhouse, Lever, Workday …)
      or fall back to the GenericApplier.
    """

    board_name = "LinkedIn"
    board_slug = "linkedin"

    async def setup(self) -> None:
        await super().setup()
        # TODO: launch Playwright, navigate to LinkedIn, log in

    async def teardown(self) -> None:
        # TODO: close Playwright browser
        await super().teardown()

    async def apply(self, job: Job) -> ApplicationResult:
        if not self.can_apply(job):
            return self._skip(job, "Not a LinkedIn job")

        try:
            if job.easy_apply:
                return await self._easy_apply(job)
            else:
                return await self._external_apply(job)
        except Exception as exc:
            return self._fail(job, str(exc))

    async def _easy_apply(self, job: Job) -> ApplicationResult:
        # TODO:
        # 1. Navigate to job.url
        # 2. Click Easy Apply button
        # 3. Fill contact / resume / screening question pages
        # 4. Submit and capture confirmation
        raise NotImplementedError("LinkedIn Easy Apply not yet implemented")

    async def _external_apply(self, job: Job) -> ApplicationResult:
        # TODO: redirect to external ATS and delegate
        raise NotImplementedError("LinkedIn external apply not yet implemented")
