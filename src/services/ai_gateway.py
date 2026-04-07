from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from src.services.prompt_registry import PromptSpec

TModel = TypeVar("TModel", bound=BaseModel)


def _extract_response_text(response) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _extract_first_json_object(raw: str) -> str | None:
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(raw[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    return None


def _safe_json_loads(raw: str, *, context: str) -> dict:
    raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(raw_clean)
    except (json.JSONDecodeError, ValueError):
        json_object = _extract_first_json_object(raw_clean)
        if not json_object:
            raise ValueError(f"No JSON returned for {context}")
        return json.loads(json_object)


def _sanitize_text_fallback(raw: str, *, field_name: str) -> str:
    cleaned = re.sub(r"```(?:markdown|md|text)?\s*|\s*```", "", raw).strip()
    lines = cleaned.splitlines()
    while lines:
        first = lines[0].strip()
        lowered = first.lower().rstrip(":")
        if (
            len(first) <= 120
            and (
                lowered.startswith("here is")
                or lowered.startswith("here's")
                or lowered.startswith("below is")
                or lowered.startswith("tailored resume")
                or lowered.startswith("rewritten resume")
                or lowered.startswith("cover letter")
            )
        ):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
            continue
        break

    cleaned = "\n".join(lines).strip()
    if field_name == "cover_letter" and cleaned.startswith("Dear Hiring Team,"):
        return cleaned
    return cleaned


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
    text_fallback_field: str | None = None,
    text_fallback_min_length: int = 0,
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
    raw_text = _extract_response_text(response)
    parse_error: Exception | None = None
    try:
        parsed = response_model.model_validate(_safe_json_loads(raw_text, context=context))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        parse_error = exc
        if not text_fallback_field:
            raise
        fallback_text = _sanitize_text_fallback(raw_text, field_name=text_fallback_field)
        if len(fallback_text.strip()) < text_fallback_min_length:
            raise
        parsed = response_model.model_validate({text_fallback_field: fallback_text})

    if cache_backend is not None and source_hash:
        cache_metadata = dict(metadata or {})
        if parse_error is not None and text_fallback_field:
            cache_metadata["parser_mode"] = "text_fallback"
        await cache_backend.upsert_semantic_cache(
            _cache_key(spec, source_hash, model),
            kind=spec.kind,
            source_hash=source_hash,
            payload=parsed.model_dump(mode="json"),
            user_id=user_id,
            prompt_name=spec.name,
            prompt_version=spec.version,
            model_name=model,
            metadata=cache_metadata or None,
        )
    return parsed
