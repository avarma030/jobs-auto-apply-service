from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.ai_contracts import TailoredResumeResult
from src.services.ai_gateway import call_json_prompt
from src.services.prompt_registry import RESUME_TAILOR_PROMPT


def make_client(*texts: str) -> AsyncMock:
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text=text) for text in texts]
        )
    )
    return client


@pytest.mark.asyncio
async def test_call_json_prompt_uses_text_fallback_for_plain_text_tailoring_response():
    client = make_client(
        "Here is the tailored resume:\n\nJane Doe\nData Scientist\nExperience\nBuilt Python analytics pipelines."
    )

    result = await call_json_prompt(
        client,
        spec=RESUME_TAILOR_PROMPT,
        prompt="unused",
        model="claude-sonnet-4-6",
        max_tokens=1000,
        response_model=TailoredResumeResult,
        context="resume tailoring",
        text_fallback_field="tailored_resume_text",
        text_fallback_min_length=20,
    )

    assert result.tailored_resume_text.startswith("Jane Doe")
    assert "Here is the tailored resume" not in result.tailored_resume_text


@pytest.mark.asyncio
async def test_call_json_prompt_joins_multiple_text_blocks_before_parsing():
    client = make_client(
        "",
        '{"tailored_resume_text":"Jane Doe\\nExperience","change_summary":["Added keywords"]}',
    )

    result = await call_json_prompt(
        client,
        spec=RESUME_TAILOR_PROMPT,
        prompt="unused",
        model="claude-sonnet-4-6",
        max_tokens=1000,
        response_model=TailoredResumeResult,
        context="resume tailoring",
    )

    assert result.tailored_resume_text == "Jane Doe\nExperience"
    assert result.change_summary == ["Added keywords"]
