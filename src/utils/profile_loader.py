from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from src.models import UserProfile


def load_profile(path: Path | str) -> UserProfile:
    """Load a UserProfile from a JSON file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"User profile not found at {path}. "
            "Copy data/user_profile.example.json to data/user_profile.json and fill in your details, "
            "or run: python main.py extract-profile --resume data/resume.pdf"
        )
    with path.open() as f:
        data = json.load(f)
    profile = UserProfile(**data)
    logger.info(f"Loaded profile for {profile.first_name} {profile.last_name}")
    return profile


def save_profile(profile: UserProfile, path: Path | str) -> None:
    """Serialise a UserProfile back to a JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2))
    logger.info(f"Saved profile for {profile.first_name} {profile.last_name} → {path}")
