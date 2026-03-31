from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from src.services.prompt_registry import PromptSpec

TModel = TypeVar("TModel", bound=BaseModel)


def _safe_json_loads(raw: str, *, context: str) -> dict:
    raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(raw_clean)
    except (json.JSONDecodeError, ValueError):
        json_match = re.search(r"\{.*\}", raw_clean, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON returned for {context}")
        return json.loads(json_match.group())


def _cache_key(spec: PromptSpec, source_hash: str, model: str) -> str:
    digest = sha256()
    digest.update(spec.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(spec.version.encode("utf-8"))
    digest.update(b"\0")
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(source_hash.encode("utf-8"))
    return f"prompt:{spec.name}:{spec.version}:{digest.hexdigest()[:24]}"


async def call_json_prompt(
    client: anthropic.AsyncAnthropic,
    *,
    spec: PromptSpec,
    prompt: str,
    model: str,
    max_tokens: int,
    response_model: type[TModel],
    context: str,
    cache_backend=None,
    source_hash: str | None = None,
    user_id: int | None = None,
    metadata: dict | None = None,
) -> TModel:
    if cache_backend is not None and source_hash:
        cached = await cache_backend.get_semantic_cache(
            _cache_key(spec, source_hash, model),
            source_hash,
        )
        if cached is not None:
            try:
                return response_model.model_validate(cached)
            except ValidationError:
                pass

    response = await client.messages.create(
        model=model,
        system=spec.system,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = response_model.model_validate(
        _safe_json_loads(response.content[0].text.strip(), context=context)
    )

    if cache_backend is not None and source_hash:
        await cache_backend.upsert_semantic_cache(
            _cache_key(spec, source_hash, model),
            kind=spec.kind,
            source_hash=source_hash,
            payload=parsed.model_dump(mode="json"),
            user_id=user_id,
            prompt_name=spec.name,
            prompt_version=spec.version,
            model_name=model,
            metadata=metadata,
        )
    return parsed
