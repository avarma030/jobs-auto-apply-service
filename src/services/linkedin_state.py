from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


_LINKEDIN_STATE_ROOT = Path("data/linkedin")
_LEGACY_COOKIE_PATH = Path("data/.linkedin_cookies.json")
_LEGACY_SESSION_DIRS = {
    "applier": Path("data/.linkedin_session"),
    "scraper": Path("data/.linkedin_scraper_session"),
    "detail": Path("data/.linkedin_detail_session"),
}
_LINKEDIN_2FA_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name='pin']",
    "input[name='verificationCode']",
    "input#input__phone_verification_pin",
    "input#checkpoint-pin",
)
_LINKEDIN_2FA_TEXT_PATTERNS = (
    "approve sign in",
    "approve your sign in",
    "approve this sign in",
    "approve this login",
    "check your linkedin app",
    "open your linkedin app",
    "tap yes on your mobile device",
    "click yes on your mobile device",
    "confirm your sign in attempt",
    "verify your sign-in attempt",
    "use your mobile device",
    "two-step verification",
    "two step verification",
    "verification code",
    "enter the verification code",
    "enter the code we sent",
    "confirm this is you",
)
_LINKEDIN_CHECKPOINT_SELECTORS = (
    "iframe[title*='captcha']",
    "iframe[src*='captcha']",
    "div.recaptcha-checkbox-border",
    "#captcha-internal",
)
_LINKEDIN_CHECKPOINT_TEXT_PATTERNS = (
    "security checkpoint",
    "security check",
    "let's do a quick security check",
    "prove you're human",
    "verify that you're human",
    "captcha",
)


@dataclass(frozen=True)
class LinkedInAuthChallenge:
    kind: str
    message: str


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


def linkedin_two_factor_auth_guidance() -> str:
    return (
        "LinkedIn requested manual verification or 2-factor authentication "
        "(for example, approving the login on your mobile device). Disable "
        "2-factor authentication for the LinkedIn account used by this automation, "
        "then retry."
    )


def linkedin_two_factor_auth_message() -> str:
    return (
        "LinkedIn requires 2-factor authentication or mobile approval for this account. "
        + linkedin_two_factor_auth_guidance()
    )


def linkedin_checkpoint_auth_message() -> str:
    return "LinkedIn presented a security checkpoint or CAPTCHA for this account."


async def _linkedin_page_text(page: Page) -> str:
    try:
        body = page.locator("body")
        if await body.count() == 0:
            return ""
        return " ".join((await body.inner_text()).split()).lower()
    except Exception:
        return ""


async def _linkedin_has_visible_selector(page: Page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


async def detect_linkedin_auth_challenge(page: Page) -> LinkedInAuthChallenge | None:
    url = str(getattr(page, "url", "") or "").lower()
    page_text = await _linkedin_page_text(page)

    if (
        any(token in url for token in ("/checkpoint", "/challenge", "two-step"))
        or any(pattern in page_text for pattern in _LINKEDIN_2FA_TEXT_PATTERNS)
        or await _linkedin_has_visible_selector(page, _LINKEDIN_2FA_SELECTORS)
    ):
        if (
            any(pattern in page_text for pattern in _LINKEDIN_2FA_TEXT_PATTERNS)
            or await _linkedin_has_visible_selector(page, _LINKEDIN_2FA_SELECTORS)
        ):
            return LinkedInAuthChallenge(
                kind="2fa_required",
                message=linkedin_two_factor_auth_message(),
            )

    if (
        any(token in url for token in ("/checkpoint", "/challenge", "captcha"))
        or any(pattern in page_text for pattern in _LINKEDIN_CHECKPOINT_TEXT_PATTERNS)
        or await _linkedin_has_visible_selector(page, _LINKEDIN_CHECKPOINT_SELECTORS)
    ):
        return LinkedInAuthChallenge(
            kind="checkpoint_or_captcha",
            message=linkedin_checkpoint_auth_message(),
        )

    return None
