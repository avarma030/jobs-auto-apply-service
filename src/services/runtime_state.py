from __future__ import annotations

import shutil
from pathlib import Path

from src.config import settings


_UPLOAD_ROOT = Path("data/uploads")


def user_upload_dir(user_id: int) -> Path:
    return _UPLOAD_ROOT / str(user_id)


def user_resume_path(user_id: int) -> Path:
    return user_upload_dir(user_id) / "resume.pdf"


def user_tailored_dir(user_id: int, job_id: int) -> Path:
    return user_upload_dir(user_id) / "tailored" / str(job_id)


def ensure_user_upload_dir(user_id: int) -> Path:
    path = user_upload_dir(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_cli_resume_mirror(source: Path) -> None:
    settings.resume_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, settings.resume_path)


def write_cli_profile_mirror(profile_json: str) -> None:
    settings.user_profile_path.parent.mkdir(parents=True, exist_ok=True)
    settings.user_profile_path.write_text(profile_json, encoding="utf-8")
