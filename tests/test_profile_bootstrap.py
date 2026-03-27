from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from src.cli import main
from src.utils.profile_bootstrap import (
    bootstrap_profile_from_resume,
    build_profile_from_resume_text,
)
from src.utils.profile_loader import load_profile

RESUME_TEXT = """
Alice Johnson
Senior Product Manager
Dublin, Ireland
alice.johnson@example.com | +353 87 123 4567
https://linkedin.com/in/alicejohnson | https://github.com/alicejohnson | https://alicepm.dev

SUMMARY
Product leader with 8 years of experience launching SaaS products across B2B workflows.

SKILLS
Product Strategy, Roadmapping, SQL, Python, Stakeholder Management, User Research

EXPERIENCE
Senior Product Manager
Acme Corp
Jan 2022 - Present
Dublin, Ireland
Led the launch of an enterprise workflow platform and a new analytics suite.

Product Manager
Beta Ltd
Mar 2018 - Dec 2021
Remote
Owned discovery, roadmap planning, experimentation, and quarterly planning.

EDUCATION
Trinity College Dublin
Bachelor of Science in Business Information Systems
2014 - 2018

LANGUAGES
English, French
""".strip()


def test_build_profile_from_resume_text_extracts_core_fields() -> None:
    result = build_profile_from_resume_text(RESUME_TEXT, resume_path=Path("data/resume.txt"))

    profile = result.profile
    assert profile.first_name == "Alice"
    assert profile.last_name == "Johnson"
    assert profile.email == "alice.johnson@example.com"
    assert profile.phone == "+353 87 123 4567"
    assert profile.address and profile.address.city == "Dublin"
    assert profile.address.country == "Ireland"
    assert profile.headline == "Senior Product Manager"
    assert profile.summary and "8 years of experience" in profile.summary
    assert profile.skills[:3] == ["Product Strategy", "Roadmapping", "SQL"]
    assert profile.languages == ["English", "French"]
    assert profile.social_links.linkedin == "https://linkedin.com/in/alicejohnson"
    assert profile.social_links.github == "https://github.com/alicejohnson"
    assert profile.social_links.portfolio == "https://alicepm.dev"
    assert profile.years_of_experience == 8

    assert len(profile.work_experience) == 2
    assert profile.work_experience[0].title == "Senior Product Manager"
    assert profile.work_experience[0].company == "Acme Corp"
    assert profile.work_experience[0].start_date == "2022-01"
    assert profile.work_experience[0].end_date is None
    assert profile.work_experience[0].location == "Dublin, Ireland"

    assert len(profile.education) == 1
    assert profile.education[0].institution == "Trinity College Dublin"
    assert profile.education[0].degree == "Bachelor of Science in Business Information Systems"
    assert "skills" in result.extracted_fields
    assert not result.review_notes


def test_bootstrap_profile_from_resume_writes_json(tmp_path: Path) -> None:
    resume_path = tmp_path / "resume.txt"
    output_path = tmp_path / "user_profile.json"
    resume_path.write_text(RESUME_TEXT, encoding="utf-8")

    result = bootstrap_profile_from_resume(resume_path, output_path=output_path)

    assert result.output_path == output_path
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["resume_path"] == str(resume_path)
    assert payload["first_name"] == "Alice"
    assert payload["social_links"]["portfolio"] == "https://alicepm.dev"


def test_profile_bootstrap_cli_generates_profile(tmp_path: Path) -> None:
    runner = CliRunner()
    resume_path = tmp_path / "resume.txt"
    output_path = tmp_path / "generated_profile.json"
    resume_path.write_text(RESUME_TEXT, encoding="utf-8")

    result = runner.invoke(
        main,
        [
            "profile",
            "bootstrap",
            "--resume",
            str(resume_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "Generated profile draft" in result.output
    assert "Extracted fields" in result.output


def test_load_profile_missing_message_mentions_bootstrap(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    try:
        load_profile(missing_path)
    except FileNotFoundError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected a missing profile to raise FileNotFoundError")

    assert "profile bootstrap" in message
