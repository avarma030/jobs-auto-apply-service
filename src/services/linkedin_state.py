from __future__ import annotations

import hashlib
import re
from pathlib import Path


_LINKEDIN_STATE_ROOT = Path("data/linkedin")
_LEGACY_COOKIE_PATH = Path("data/.linkedin_cookies.json")
_LEGACY_SESSION_DIRS = {
    "applier": Path("data/.linkedin_session"),
    "scraper": Path("data/.linkedin_scraper_session"),
    "detail": Path("data/.linkedin_detail_session"),
}


def linkedin_account_key(username: str | None) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        return "default"
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:40] or "user"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def linkedin_cookie_path(username: str | None) -> Path:
    return _LINKEDIN_STATE_ROOT / linkedin_account_key(username) / "cookies.json"


def linkedin_session_dir(username: str | None, kind: str) -> Path:
    return _LINKEDIN_STATE_ROOT / linkedin_account_key(username) / f"{kind}_session"


def legacy_linkedin_cookie_path() -> Path:
    return _LEGACY_COOKIE_PATH


def legacy_linkedin_session_dir(kind: str) -> Path:
    return _LEGACY_SESSION_DIRS[kind]


def mask_linkedin_username(username: str | None) -> str:
    normalized = (username or "").strip()
    if not normalized:
        return "default"
    if "@" in normalized:
        local, domain = normalized.split("@", 1)
        if len(local) <= 2:
            local_masked = f"{local[:1]}*"
        else:
            local_masked = f"{local[:2]}***"
        return f"{local_masked}@{domain}"
    if len(normalized) <= 4:
        return normalized[:1] + "***"
    return f"{normalized[:3]}***"
