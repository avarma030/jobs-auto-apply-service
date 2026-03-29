from __future__ import annotations

from typing import Any

from src.models import UserProfile


def merge_profile_data(primary: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    merged: dict[str, Any] = {}

    for key in set(fallback) | set(primary):
        primary_value = primary.get(key)
        fallback_value = fallback.get(key)

        if isinstance(primary_value, dict) and isinstance(fallback_value, dict):
            merged_value = merge_profile_data(primary_value, fallback_value)
        elif isinstance(primary_value, list):
            merged_value = primary_value if primary_value else fallback_value
        elif not _is_empty_profile_value(primary_value):
            merged_value = primary_value
        else:
            merged_value = fallback_value

        if not _is_empty_profile_value(merged_value):
            merged[key] = merged_value

    return merged


def sanitize_profile_data(raw: dict[str, Any], *, resume_path: str | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}

    address_raw = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    address = _compact_dict(
        {
            "street": _clean_string(address_raw.get("street")),
            "city": _clean_string(address_raw.get("city")),
            "state": _clean_string(address_raw.get("state")),
            "zip_code": _clean_string(address_raw.get("zip_code")),
            "country": _clean_string(address_raw.get("country")) or "US",
        }
    )

    social_links_raw = raw.get("social_links") if isinstance(raw.get("social_links"), dict) else {}
    social_links = _compact_dict(
        {
            "linkedin": _clean_string(social_links_raw.get("linkedin")),
            "github": _clean_string(social_links_raw.get("github")),
            "portfolio": _clean_string(social_links_raw.get("portfolio")),
            "twitter": _clean_string(social_links_raw.get("twitter")),
        }
    )

    custom_answers_raw = raw.get("custom_answers") if isinstance(raw.get("custom_answers"), dict) else {}
    custom_answers = {
        question: answer
        for raw_question, raw_answer in custom_answers_raw.items()
        if (question := _clean_string(raw_question)) is not None
        if (answer := _clean_string(raw_answer)) is not None
    }

    job_board_accounts_raw = (
        raw.get("job_board_accounts") if isinstance(raw.get("job_board_accounts"), dict) else {}
    )
    job_board_accounts = {
        board: creds
        for raw_board, raw_creds in job_board_accounts_raw.items()
        if (board := _clean_string(raw_board)) is not None
        if isinstance(raw_creds, dict)
        if (
            creds := _compact_dict(
                {
                    "username": _clean_string(raw_creds.get("username")),
                    "password": _clean_string(raw_creds.get("password")),
                    "access_token": _clean_string(raw_creds.get("access_token")),
                }
            )
        )
    }

    preferences_raw = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    preferences = _compact_dict(
        {
            "auto_apply": _coerce_bool(preferences_raw.get("auto_apply")),
            "require_confirmation": _coerce_bool(preferences_raw.get("require_confirmation")),
            "max_applications_per_day": _coerce_int(preferences_raw.get("max_applications_per_day")),
            "easy_apply_only": _coerce_bool(preferences_raw.get("easy_apply_only")),
            "skip_if_salary_not_listed": _coerce_bool(preferences_raw.get("skip_if_salary_not_listed")),
            "preferred_work_modes": _clean_string_list(preferences_raw.get("preferred_work_modes")),
            "blacklisted_companies": _clean_string_list(preferences_raw.get("blacklisted_companies")),
            "whitelisted_companies": _clean_string_list(preferences_raw.get("whitelisted_companies")),
        }
    )

    work_experience = []
    for item in raw.get("work_experience") or []:
        if not isinstance(item, dict):
            continue
        company = _clean_string(item.get("company"))
        title = _clean_string(item.get("title"))
        start_date = _clean_string(item.get("start_date"))
        end_date = _clean_string(item.get("end_date"))
        description = _clean_string(item.get("description"))
        location = _clean_string(item.get("location"))
        if all(value is None for value in (company, title, start_date, end_date, description, location)):
            continue
        entry = {
            "company": company or "",
            "title": title or "",
            "start_date": start_date or "",
        }
        if end_date is not None:
            entry["end_date"] = end_date
        if description is not None:
            entry["description"] = description
        if location is not None:
            entry["location"] = location
        work_experience.append(entry)

    education = []
    for item in raw.get("education") or []:
        if not isinstance(item, dict):
            continue
        institution = _clean_string(item.get("institution"))
        degree = _clean_string(item.get("degree"))
        field_of_study = _clean_string(item.get("field_of_study"))
        start_date = _clean_string(item.get("start_date"))
        end_date = _clean_string(item.get("end_date"))
        gpa = _coerce_float(item.get("gpa"))
        if all(
            value is None
            for value in (institution, degree, field_of_study, start_date, end_date, gpa)
        ):
            continue
        entry = {
            "institution": institution or "",
            "degree": degree or "",
        }
        if field_of_study is not None:
            entry["field_of_study"] = field_of_study
        if start_date is not None:
            entry["start_date"] = start_date
        if end_date is not None:
            entry["end_date"] = end_date
        if gpa is not None:
            entry["gpa"] = gpa
        education.append(entry)

    profile_data: dict[str, Any] = {
        "first_name": _clean_string(raw.get("first_name")) or "User",
        "last_name": _clean_string(raw.get("last_name")) or "",
        "email": _clean_string(raw.get("email")) or "",
    }
    optional_fields = _compact_dict(
        {
            "phone": _clean_string(raw.get("phone")),
            "address": address or None,
            "headline": _clean_string(raw.get("headline")),
            "summary": _clean_string(raw.get("summary")),
            "years_of_experience": _coerce_int(raw.get("years_of_experience")),
            "skills": _clean_string_list(raw.get("skills")),
            "languages": _clean_string_list(raw.get("languages")),
            "work_experience": work_experience,
            "education": education,
            "social_links": social_links or None,
            "resume_path": resume_path or _clean_string(raw.get("resume_path")),
            "cover_letter_template_path": _clean_string(raw.get("cover_letter_template_path")),
            "job_board_accounts": job_board_accounts or None,
            "preferences": preferences or None,
            "custom_answers": custom_answers,
        }
    )
    profile_data.update(optional_fields)

    profile = UserProfile(**profile_data)
    return profile.model_dump(mode="json", exclude_none=True)


def build_user_profile(raw: dict[str, Any], *, resume_path: str | None = None) -> UserProfile:
    return UserProfile(**sanitize_profile_data(raw, resume_path=resume_path))


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _clean_string(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if not _is_empty_profile_value(item)
    }


def _is_empty_profile_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False
