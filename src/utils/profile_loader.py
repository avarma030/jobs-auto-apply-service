from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.models import UserProfile


def load_profile(path: Path | str) -> UserProfile:
    """Load a UserProfile from a JSON file."""
    from src.models import UserProfile

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"User profile not found at {path}. "
            "Copy data/user_profile.example.json to data/user_profile.json and fill in your details."
        )
    with path.open() as f:
        data = json.load(f)
    profile = UserProfile(**data)
    logger.info(f"Loaded profile for {profile.first_name} {profile.last_name}")
    return profile
