from __future__ import annotations

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import Job


class GenericApplier(BaseApplier):
    """Best-effort applier for unknown / unsupported ATS platforms.

    Uses heuristics to detect common form fields (name, email, resume upload)
    and fill them using the user profile. Not all applications will succeed.
    """

    board_name = "Generic"
    board_slug = "generic"

    def can_apply(self, job: Job) -> bool:
        return True  # fallback — handles anything

    async def apply(self, job: Job) -> ApplicationResult:
        try:
            # TODO:
            # 1. Navigate to job.url
            # 2. Detect apply button / form
            # 3. Map form fields to profile data via label heuristics
            # 4. Upload resume if file input detected
            # 5. Submit and capture any confirmation text
            raise NotImplementedError("Generic applier not yet implemented")
        except Exception as exc:
            return self._fail(job, str(exc))
