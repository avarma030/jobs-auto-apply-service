from pathlib import Path

from src.services.profile_sanitizer import build_user_profile, merge_profile_data, sanitize_profile_data


def test_merge_profile_data_prefers_new_resume_values_and_keeps_existing_nested_settings():
    extracted = {
        "first_name": "Rucha",
        "last_name": "Varma",
        "email": "rucha@example.com",
        "address": {"city": "Dublin"},
    }
    existing = {
        "first_name": "Akshay",
        "job_board_accounts": {"linkedin": {"username": "old@example.com", "password": "secret"}},
        "custom_answers": {"Are you authorized to work?": "Yes"},
    }

    merged = merge_profile_data(extracted, existing)

    assert merged["first_name"] == "Rucha"
    assert merged["address"]["city"] == "Dublin"
    assert merged["job_board_accounts"]["linkedin"]["username"] == "old@example.com"
    assert merged["custom_answers"]["Are you authorized to work?"] == "Yes"


def test_sanitize_profile_data_handles_partial_education_without_validation_errors():
    sanitized = sanitize_profile_data(
        {
            "first_name": "Rucha",
            "last_name": "Varma",
            "email": "rucha@example.com",
            "education": [
                {
                    "institution": None,
                    "degree": "MBA",
                    "field_of_study": "Project Management",
                }
            ],
        },
        resume_path="data/uploads/2/resume.pdf",
    )

    assert Path(sanitized["resume_path"]).as_posix() == "data/uploads/2/resume.pdf"
    assert sanitized["education"][0]["institution"] == ""
    assert sanitized["education"][0]["degree"] == "MBA"


def test_build_user_profile_salvages_invalid_nested_profile_data():
    profile = build_user_profile(
        {
            "first_name": "Rucha",
            "last_name": "Varma",
            "email": "rucha@example.com",
            "address": {"city": "Dublin", "country": "IE"},
            "education": [{"institution": None, "degree": "MBA"}],
            "work_experience": [{"company": "Acme", "title": "PM", "start_date": None}],
        }
    )

    assert profile.first_name == "Rucha"
    assert profile.address is not None
    assert profile.address.city == "Dublin"
    assert profile.education[0].institution == ""
    assert profile.work_experience[0].company == "Acme"
