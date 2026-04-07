from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import resume_tailor


def make_client(text: str) -> AsyncMock:
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text=text)])
    )
    return client


@pytest.mark.asyncio
async def test_tailor_resume_accepts_plain_text_model_output_without_failing():
    client = make_client(
        "Here is the tailored resume:\n\n"
        "Jane Doe\n"
        "Data Scientist\n"
        "Skills\n"
        "Python, SQL, Statistics\n"
        "Experience\n"
        "Built analytics and machine learning workflows."
    )

    with patch.object(resume_tailor, "score_ats", new=AsyncMock(return_value=92.0)):
        tailored_text, ats_score = await resume_tailor.tailor_resume(
            resume_text="Jane Doe\nOriginal resume text",
            job_title="Data Scientist",
            job_description="Need Python, SQL, and statistics experience.",
            client=client,
            max_attempts=1,
        )

    assert tailored_text.startswith("Jane Doe")
    assert "Here is the tailored resume" not in tailored_text
    assert ats_score == 92.0
