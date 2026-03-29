from __future__ import annotations

import re
import unicodedata


def normalize_question_key(text: str | None) -> str:
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(text))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def normalize_question_text(text: str | None) -> str:
    if text is None:
        return ""

    lines = re.split(r"[\r\n]+", str(text).replace("\u00a0", " "))
    cleaned_lines: list[str] = []
    seen_keys: set[str] = set()

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip(" \t:-*•")
        if not line:
            continue

        line_key = normalize_question_key(line)
        if line_key in {"required", "optional"}:
            continue
        if line_key and line_key in seen_keys:
            continue

        cleaned_lines.append(line)
        if line_key:
            seen_keys.add(line_key)

    question = re.sub(r"\s+", " ", " ".join(cleaned_lines)).strip()
    question = re.sub(r"(?:\s+[-:])?(?:required|optional)\s*$", "", question, flags=re.I).strip()
    return question


def semantic_yes_no(text: str | None) -> str | None:
    key = normalize_question_key(text)
    if key in {"yes", "y", "true", "1", "si", "sure", "available"}:
        return "yes"
    if key in {"no", "n", "false", "0"}:
        return "no"
    return None
