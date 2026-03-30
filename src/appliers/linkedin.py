"""LinkedIn applier â€” Easy Apply automation via Playwright.

Flow
----
1. ``setup()``  â€” launch Playwright browser; inject warm scraper cookies; verify
                  session or perform fresh login.
2. ``apply()``  â€” navigate to the job page, click "Easy Apply", step through
                  the modal, and submit.

Login strategy (most â†’ least expensive)
----------------------------------------
1. Load ``data/.linkedin_cookies.json`` (written by the scraper after warming) and
   inject directly into the browser context â€” cheapest, no form interaction.
2. Navigate to /feed; if session is live, done.
3. If not logged in, perform a full form-based login with human-like delays.
4. Save fresh cookies back to disk so the next run skips login again.

Easy Apply modal steps (order may vary per job):
  â€¢ Contact info  â€” phone, email (usually pre-filled from account)
  â€¢ Resume        â€” select "Use a resume" or upload file
  â€¢ Screening Q's â€” text, number, dropdown, yes/no radio, checkbox
  â€¢ Cover letter  â€” optional textarea
  â€¢ Review        â€” summary page
  â€¢ Submit        â€” final button

External-apply jobs are detected and skipped (returns SKIPPED result);
callers should route them to the appropriate ATS applier.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Locator, Page, TimeoutError as PWTimeoutError

from src.appliers.base import (
    AnsweredQuestion,
    ApplicationQuestionPrompt,
    ApplicationResult,
    BaseApplier,
)
from src.config import settings
from src.models import Job, UserProfile
from src.services.application_questions import (
    normalize_question_key,
    normalize_question_text,
    semantic_yes_no,
)
from src.services.linkedin_state import (
    detect_linkedin_auth_challenge,
    legacy_linkedin_cookie_path,
    linkedin_cookie_path,
    linkedin_session_dir,
    mask_linkedin_username,
)
from src.utils.browser import BrowserManager, BrowserManager as _BM

# â”€â”€ Cookie / session paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_COOKIE_MAX_AGE = 4 * 3600          # reuse cookies up to 4 hours old

# â”€â”€ URLs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_LOGIN_URL = "https://www.linkedin.com/login"
_FEED_URL  = "https://www.linkedin.com/feed/"

# â”€â”€ Login selectors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Verified against LinkedIn login page source.
# name= attributes are tied to the form POST and are the most stable.
# id= values are a reliable secondary fallback.
_EMAIL_SEL = "input#username, input[name='session_key']:not([type='hidden'])"
_PASS_SEL  = "input#password, input[name='session_password']:not([type='hidden'])"

# Presence of any of these means the session is authenticated.
_LOGGED_IN_SELECTORS = [
    "div.global-nav__me-photo",
    "img.global-nav__me-photo",
    "div.feed-identity-module",
    "nav.global-nav",
    "div[data-control-name='identity_welcome_message']",
]

# â”€â”€ Job page selectors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# "Follow" prompt that sometimes blocks the Easy Apply button
_FOLLOW_PROMPT_DISMISS = (
    "button[aria-label='Dismiss'], "
    "button[aria-label='Got it'], "
    "button.artdeco-modal__dismiss"
)
_AUTOCOMPLETE_RESULTS = (
    "div[data-test-single-typeahead-entity-form-search-result='true'], "
    ".search-typeahead-v2__hit--autocomplete"
)

# Job page â€” aria-label based selectors survive LinkedIn UI/class renames
# Broad selector fallback for legacy DOMs. Primary detection uses role/name.
_EASY_APPLY_BTN = (
    "button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button, "
    "a.jobs-apply-button"
)
_APPLY_BTN = (
    "button.jobs-apply-button, "
    "button[data-control-name='jobdetails_topcard_inapply'], "
    "a.jobs-apply-button"
)

# Already-applied state indicators
_ALREADY_APPLIED = (
    "span.artdeco-inline-feedback--success, "
    "div.post-apply-timeline__entity, "
    "button[aria-label*='Applied']"
)
_ALREADY_APPLIED_TEXT = re.compile(r"application submitted|already applied|applied on", re.I)
_JOB_SEARCH_SAFETY_REMINDER_TEXT = re.compile(r"job search safety reminder", re.I)

# â”€â”€ Modal selectors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_MODAL = "div.jobs-easy-apply-modal, div.jobs-apply-modal, div[role='dialog']"

_STEP_INDICATOR = (
    "span.jobs-easy-apply-form-section__grouping-title, "
    "div.ph5 span.t-14"
)

_NEXT_BTN = (
    "button[aria-label='Continue to next step'], "
    "button[aria-label='Review your application'], "
    "footer button[aria-label*='Next'], "
    "button.artdeco-button--primary[type='button']"
)
_SUBMIT_BTN  = "button[aria-label='Submit application']"
_DISMISS_BTN = "button[aria-label='Dismiss']"
_ERROR_MSG   = "div.artdeco-inline-feedback--error, p.artdeco-inline-feedback__message"

# Form field selectors (inside modal)
_TEXT_INPUTS = "input[type='text'], input[type='email'], input[type='tel'], input[type='number']"
_TEXTAREAS   = "textarea"
_SELECTS     = "select"
_RADIO_GROUPS = "fieldset"
_CHECKBOXES  = "input[type='checkbox']"

# Resume upload
_FILE_INPUT  = "input[type='file']"
_RESUME_CARD = (
    "div.jobs-document-upload__card, "
    "label.jobs-document-upload-redesign-card, "
    "li.jobs-resume-picker__resume"
)
_DOCUMENT_UPLOAD_HINTS = (
    "resume",
    "cv",
    "curriculum vitae",
    "cover letter",
    "document",
    "attachment",
    "portfolio",
)
_IMAGE_UPLOAD_HINTS = (
    "photo",
    "picture",
    "image",
    "avatar",
    "headshot",
    "profile picture",
    "profile photo",
)


class LinkedInApplier(BaseApplier):
    """Applies to LinkedIn Easy Apply jobs using Playwright browser automation."""

    board_name = "LinkedIn"
    board_slug = "linkedin"

    def __init__(self, profile: UserProfile):
        super().__init__(profile)
        self._bm: BrowserManager | None = None
        self._page: Page | None = None
        self._logged_in = False
        self._unknown_question_prompts: dict[str, ApplicationQuestionPrompt] = {}
        self._answered_questions: list[AnsweredQuestion] = []
        self._learned_answers: dict[str, str] = {}
        self._answered_question_index: dict[str, int] = {}
        self._cookie_path = linkedin_cookie_path(None)
        self._session_dir = linkedin_session_dir(None, "applier")
        self._linkedin_username = ""
        self._linkedin_password = ""
        self._linkedin_identity = ""
        self._linkedin_credential_source = "unknown"
        self._auth_error: str | None = None

    def _resolve_linkedin_credentials(self) -> tuple[str, str, str]:
        creds = self.profile.job_board_accounts.linkedin
        username = (creds.username or "").strip() if creds else ""
        password = (creds.password or "").strip() if creds else ""
        source = "profile"
        if (not username or not password) and settings.linkedin_email:
            username = settings.linkedin_email.strip()
            password = (settings.linkedin_password or "").strip()
            source = "environment"
        return username, password, source

    def _ensure_linkedin_state(self) -> None:
        username, password, source = self._resolve_linkedin_credentials()
        identity = username or (self.profile.email or "").strip()
        if not identity:
            identity = "default"
        self._linkedin_username = username
        self._linkedin_password = password
        self._linkedin_identity = identity
        self._linkedin_credential_source = source
        self._cookie_path = linkedin_cookie_path(identity)
        self._session_dir = linkedin_session_dir(identity, "applier")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        await super().setup()
        self._ensure_linkedin_state()
        self._auth_error = None
        # 60 s timeout â€” LinkedIn pages can be slow; 30 s caused spurious failures.
        self._bm = BrowserManager(
            user_data_dir=self._session_dir,
            timeout_ms=60,          # passed to BrowserManager as seconds â†’ *1000 inside
        )
        await self._bm.start()
        self._page = await self._bm.new_page()
        await self._ensure_logged_in()

    async def teardown(self) -> None:
        if self._bm:
            await self._bm.stop()
        await super().teardown()

    # ------------------------------------------------------------------
    # Public apply entry point
    # ------------------------------------------------------------------

    async def apply(
        self,
        job: Job,
        tailored_resume_path: str | None = None,
        cover_letter: str | None = None,
    ) -> ApplicationResult:
        if not self.can_apply(job):
            return self._skip(job, "Not a LinkedIn job")
        if not self._page:
            return self._fail(job, "Browser not initialised")
        if self._auth_error:
            return self._fail(job, self._auth_error)

        # Re-verify session before each application â€” handles long pipelines
        # where cookies expire mid-run.
        if not self._logged_in or not await self._is_logged_in(self._page):
            logger.info("[LinkedIn] Session appears stale â€” re-authenticating â€¦")
            self._logged_in = False
            await self._ensure_logged_in()
            if not self._logged_in:
                return self._fail(
                    job,
                    self._auth_error or "LinkedIn authentication unavailable",
                )

        self._unknown_question_prompts = {}
        self._answered_questions = []
        self._learned_answers = {}
        self._answered_question_index = {}
        try:
            # Note: we do NOT gate on job.easy_apply here because the DB flag is often
            # wrong (Voyager API scrape sets description but not easy_apply).
            # _easy_apply() navigates to the actual page and checks for the button live â€”
            # that is the authoritative check. If there's no Easy Apply button, it skips.
            result = await self._easy_apply(
                job,
                tailored_resume_path=tailored_resume_path,
                cover_letter=cover_letter,
            )
        except Exception as exc:
            await self._bm.screenshot(self._page, f"error_{job.external_id}")
            result = self._fail(job, str(exc))

        result.new_question_prompts = list(self._unknown_question_prompts.values())
        result.new_questions = [prompt.question for prompt in result.new_question_prompts]
        result.answered_questions = list(self._answered_questions)
        result.learned_answers = dict(self._learned_answers)
        self._unknown_question_prompts = {}
        self._answered_questions = []
        self._learned_answers = {}
        self._answered_question_index = {}
        return result

    def _report_progress(self, message: str, level: str = "info") -> None:
        log_fn = getattr(logger, level, logger.info)
        log_fn(message)
        self._emit_progress(message)

    @staticmethod
    def _preview_answer(answer: str, max_len: int = 100) -> str:
        compact = " ".join(str(answer).split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[: max_len - 3]}..."

    @staticmethod
    def _clean_answer(answer: str | None) -> str | None:
        if answer is None:
            return None
        cleaned = str(answer).strip()
        return cleaned or None

    @staticmethod
    def _summarize_exception(exc: Exception) -> str:
        summary = " ".join(str(exc).splitlines()[:1]).strip()
        return summary or exc.__class__.__name__

    @staticmethod
    def _normalize_choice(text: str) -> str:
        return normalize_question_key(text)

    def _match_answer_to_options(self, answer: str, options: list[str]) -> str | None:
        if not options:
            return answer

        cleaned_answer = self._clean_answer(answer)
        if cleaned_answer is None:
            return None

        normalized_answer = self._normalize_choice(cleaned_answer)
        for option in options:
            normalized_option = self._normalize_choice(option)
            if normalized_answer == normalized_option:
                return option
        for option in options:
            normalized_option = self._normalize_choice(option)
            if normalized_answer and (
                normalized_answer in normalized_option or normalized_option in normalized_answer
            ):
                return option

        semantic_answer = semantic_yes_no(cleaned_answer)
        if semantic_answer is not None:
            for option in options:
                if semantic_yes_no(option) == semantic_answer:
                    return option
        return None

    def _remember_answer(self, question: str, answer: str) -> None:
        normalized_question = normalize_question_text(question)
        if not normalized_question:
            return True
        existing = self.profile.custom_answers.get(normalized_question)
        if existing == answer:
            return
        self.profile.custom_answers[normalized_question] = answer
        self._learned_answers[normalized_question] = answer

    def _record_answer(self, question: str, answer: str, source: str, field_type: str) -> None:
        normalized_question = normalize_question_text(question)
        cleaned_answer = self._clean_answer(answer)
        if not normalized_question or cleaned_answer is None:
            return

        existing_index = self._answered_question_index.get(normalized_question)
        updated_entry = AnsweredQuestion(
            question=normalized_question,
            answer=cleaned_answer,
            source=source,
            field_type=field_type,
        )
        if existing_index is not None:
            existing = self._answered_questions[existing_index]
            if existing.answer == cleaned_answer:
                return
            self._answered_questions[existing_index] = updated_entry
            return

        self._answered_question_index[normalized_question] = len(self._answered_questions)
        self._answered_questions.append(updated_entry)
        self._report_progress(
            f"[LinkedIn][Question][{source}] {normalized_question} -> {self._preview_answer(cleaned_answer)}"
        )

    def _record_prefilled_answer(self, question: str, answer: str, field_type: str) -> None:
        cleaned_answer = self._clean_answer(answer)
        if cleaned_answer is None:
            return
        self._record_answer(question, cleaned_answer, "prefilled", field_type)

    async def _resolve_answer(
        self,
        label: str,
        field_type: str,
        options: list[str] | None = None,
    ) -> tuple[str | None, str | None]:
        question = normalize_question_text(label)
        if not question:
            return None, None

        choice_options = [
            cleaned_option
            for option in (options or [])
            if (cleaned_option := self._clean_answer(option)) is not None
        ]

        saved_answer = self._clean_answer(self._answer_for_label(question))
        if saved_answer is not None:
            matched = self._match_answer_to_options(saved_answer, choice_options)
            if choice_options and matched is None:
                logger.debug(
                    f"[LinkedIn] Saved answer for '{question}' did not match options: {choice_options}"
                )
            else:
                answer = matched or saved_answer
                self._remember_answer(question, answer)
                return answer, "saved"

        inferred_answer = self._clean_answer(self._infer_value_from_label(question))
        if inferred_answer is not None:
            matched = self._match_answer_to_options(inferred_answer, choice_options)
            if choice_options and matched is None:
                logger.debug(
                    f"[LinkedIn] Inferred answer for '{question}' did not match options: {choice_options}"
                )
            else:
                answer = matched or inferred_answer
                self._remember_answer(question, answer)
                return answer, "inferred"

        resolver = getattr(self, "answer_resolver", None)
        if resolver is None:
            return None, None

        suggested_answers = await resolver(
            [ApplicationQuestionPrompt(question=question, field_type=field_type, options=choice_options)]
        )
        normalized_suggestions = {
            normalize_question_key(raw_question): self._clean_answer(raw_answer)
            for raw_question, raw_answer in suggested_answers.items()
        }
        suggested_answer = self._clean_answer(suggested_answers.get(question))
        if suggested_answer is None:
            suggested_answer = normalized_suggestions.get(normalize_question_key(question))
        if suggested_answer is None:
            return None, None

        matched = self._match_answer_to_options(suggested_answer, choice_options)
        if choice_options and matched is None:
            logger.debug(
                f"[LinkedIn] AI answer for '{question}' did not match options: {choice_options}"
            )
            return None, None

        answer = matched or suggested_answer
        self._remember_answer(question, answer)
        return answer, "ai"

    def _mark_unanswered(
        self,
        question: str,
        field_type: str,
        options: list[str] | None = None,
    ) -> None:
        normalized_question = normalize_question_text(question)
        if not normalized_question:
            return
        normalized_options = [
            cleaned_option
            for option in (options or [])
            if (cleaned_option := self._clean_answer(option)) is not None
        ]
        if normalized_question in self._answered_question_index:
            return
        if normalized_question in self._unknown_question_prompts:
            return
        self._unknown_question_prompts[normalized_question] = ApplicationQuestionPrompt(
            question=normalized_question,
            field_type=field_type,
            options=normalized_options,
        )
        self._report_progress(
            f"[LinkedIn][Question][unanswered] Could not determine an answer for "
            f"'{normalized_question}' ({field_type})",
            level="warning",
        )

    # ------------------------------------------------------------------
    # Session / login helpers
    # ------------------------------------------------------------------

    async def _inject_scraper_cookies(self) -> bool:
        """
        Load warm cookies written by the scraper (data/.linkedin_cookies.json)
        and inject them into the applier's browser context so we can skip the
        full login form flow. Returns True if li_at was successfully injected.
        """
        self._ensure_linkedin_state()
        candidates: list[tuple[Path, str, str | None]] = [
            (self._cookie_path, "scoped", self._linkedin_identity),
            (legacy_linkedin_cookie_path(), "legacy", self._linkedin_identity),
        ]
        for path, source, expected_owner in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
                age = time.time() - data.get("saved_at", 0)
                cookies = data.get("cookies", {})
                cookie_owner = str(data.get("username") or "").strip().lower()
                if expected_owner:
                    expected = expected_owner.strip().lower()
                    if not cookie_owner:
                        logger.info(
                            f"[LinkedIn] Ignoring {source} cookie file without LinkedIn account metadata"
                        )
                        continue
                    if cookie_owner != expected:
                        logger.info(
                            "[LinkedIn] Ignoring cached cookies for a different LinkedIn account: "
                            f"{mask_linkedin_username(cookie_owner)}"
                        )
                        continue
                if not cookies.get("li_at"):
                    logger.debug("[LinkedIn] Scraper cookies present but no li_at â€” skipping injection")
                    continue
                if age > _COOKIE_MAX_AGE:
                    self._report_progress(
                        f"[LinkedIn][Auth] Scraper cookies are {age / 3600:.1f}h old - attempting reuse"
                    )
                if source == "legacy":
                    self._report_progress(
                        "[LinkedIn][Auth] Falling back to legacy LinkedIn cookies for the same account"
                    )
                # Convert flat dict â†’ Playwright cookie objects
                pw_cookies = [
                    {
                        "name": k,
                        "value": v,
                        "domain": ".linkedin.com",
                        "path": "/",
                        # LinkedIn's frontend reads JSESSIONID to derive the CSRF token
                        # used by voyager/graphql requests. Marking it HttpOnly breaks
                        # Easy Apply because the modal/detail APIs start returning 403.
                        "httpOnly": k == "li_at",
                        "secure": True,
                        "sameSite": "None",
                    }
                    for k, v in cookies.items()
                ]
                await self._bm._context.add_cookies(pw_cookies)
                self._report_progress(
                    f"[LinkedIn][Auth] Injected {len(pw_cookies)} warm scraper cookies ({age / 60:.0f} min old)"
                )
                return True
            except Exception as exc:
                logger.warning(f"[LinkedIn] Cookie injection failed from {path}: {exc}")
        logger.debug("[LinkedIn] No authenticated scraper cookie file found â€” will log in fresh")
        return False

    async def _is_logged_in(self, page: Page) -> bool:
        """Return True if the current page shows an active authenticated session."""
        url = page.url
        # LinkedIn can serve public/authwall job pages under /jobs/*, so guard those first.
        if any(p in url for p in ("/login", "/authwall", "/checkpoint", "/challenge")):
            return False
        # URL-based check is fastest for pages that only render when authenticated.
        if any(p in url for p in ("/feed", "/mynetwork", "/messaging", "/in/")):
            return True
        # DOM-based check
        for sel in _LOGGED_IN_SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    def _set_auth_error(self, reason: str) -> bool:
        message = reason.strip()
        if not message.lower().startswith("linkedin authentication unavailable"):
            message = f"LinkedIn authentication unavailable: {message}"
        self._auth_error = message
        self._logged_in = False
        return False

    async def _ensure_logged_in(self) -> bool:
        """
        Establish an authenticated LinkedIn session via the most efficient path:
        1. Inject warm scraper cookies â†’ navigate to feed â†’ done if session is live.
        2. Perform a fresh form-based login with human-like delays.
        3. Save fresh cookies back to disk for the next run.
        """
        page = self._page
        self._ensure_linkedin_state()
        self._auth_error = None
        self._logged_in = False
        username = self._linkedin_username
        password = self._linkedin_password
        credential_source = self._linkedin_credential_source
        if username:
            masked_username = mask_linkedin_username(username)
            self._report_progress(
                f"[LinkedIn][Auth] Using {credential_source} credentials for {masked_username}"
            )

        # â”€â”€ 2. Inject warm scraper cookies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        await self._inject_scraper_cookies()

        # â”€â”€ 3. Check if already logged in â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning(f"[LinkedIn] Feed navigation failed: {exc}")

        if await self._is_logged_in(page):
            self._report_progress("[LinkedIn][Auth] Session active")
            logger.info("[LinkedIn] Session active â€” already logged in âœ“")
            self._logged_in = True
            return True
            return

        # â”€â”€ 4. Fresh login â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not username or not password:
            logger.warning(
                "[LinkedIn] No credentials available. "
                "Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD env vars, "
                "or enter them in the Profile â†’ Job Board Credentials section."
            )
            return self._set_auth_error(
                "No LinkedIn credentials are configured for the active profile."
            )
            return

        logger.info(f"[LinkedIn] Logging in as {username} â€¦")
        self._report_progress(
            f"[LinkedIn][Auth] Logging in as {mask_linkedin_username(username)}"
        )
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error(f"[LinkedIn] Could not load login page: {exc}")
            return self._set_auth_error(f"Could not load the LinkedIn login page: {exc}")
            return
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # If already redirected away from /login, check whether we're authenticated
        if "/login" not in page.url and "/checkpoint" not in page.url:
            if await self._is_logged_in(page):
                self._report_progress("[LinkedIn][Auth] Persistent profile session detected")
                logger.info("[LinkedIn] Persistent profile session detected â€” already logged in âœ“")
                self._logged_in = True
                return

        # Wait for the standard email input with a short timeout so we detect
        # checkpoint/CAPTCHA pages quickly instead of hanging for 15 s.
        form_visible = False
        try:
            await page.wait_for_selector("input#username", state="visible", timeout=8_000)
            form_visible = True
        except PWTimeoutError:
            # Login form not visible â€” three possible cases:
            # 1. Persistent profile cookie already authenticated â†’ redirected to /feed (success!)
            # 2. LinkedIn showing checkpoint/CAPTCHA â†’ cannot proceed automatically
            # 3. Network issue or unexpected page state
            # IMPORTANT: never clear_cookies() here â€” that destroys the valid li_at token
            # that was just injected from the scraper warm cookies.
            if await self._is_logged_in(page):
                logger.info("[LinkedIn] Already logged in (persistent profile active) âœ“")
                self._logged_in = True
                return
            challenge = await detect_linkedin_auth_challenge(page)
            if challenge:
                screenshot_name = (
                    "linkedin_login_2fa_required"
                    if challenge.kind == "2fa_required"
                    else "linkedin_login_checkpoint"
                )
                await self._bm.screenshot(page, screenshot_name)
                self._set_auth_error(challenge.message)
                self._report_progress(f"[LinkedIn][Auth] {challenge.message}", level="warning")
                logger.error(
                    f"[LinkedIn] {challenge.message} Current URL: {page.url}. "
                    "Automation cannot continue until LinkedIn login succeeds."
                )
                return
            await self._bm.screenshot(page, "linkedin_login_no_form")
            self._set_auth_error(f"LinkedIn login form was not visible at {page.url}.")
            logger.error(
                f"[LinkedIn] Login form not visible at {page.url} â€” cannot authenticate. "
                "Try running with HEADLESS_BROWSER=false to diagnose."
            )
            return

        if not form_visible:
            self._set_auth_error("LinkedIn login form was not visible.")
            return

        # Fill email
        email_field = page.locator("input#username").first
        await email_field.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await _BM.human_type(email_field, username)
        await asyncio.sleep(random.uniform(0.4, 0.9))

        # Fill password
        pass_field = page.locator("input#password").first
        await pass_field.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await _BM.human_type(pass_field, password)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Submit
        await page.click("button[type='submit']")

        # Wait for the post-login redirect to settle
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PWTimeoutError:
            pass
        await asyncio.sleep(2.5)

        current_url = page.url
        challenge = await detect_linkedin_auth_challenge(page)
        if challenge:
            screenshot_name = (
                "linkedin_login_2fa_required"
                if challenge.kind == "2fa_required"
                else "linkedin_login_checkpoint"
            )
            await self._bm.screenshot(page, screenshot_name)
            self._set_auth_error(challenge.message)
            self._report_progress(f"[LinkedIn][Auth] {challenge.message}", level="warning")
            logger.warning(
                f"[LinkedIn] {challenge.message} Current URL: {current_url}. "
                "Set HEADLESS_BROWSER=false only if you need to diagnose the exact challenge page."
            )
            return

        if await self._is_logged_in(page):
            self._logged_in = True
            logger.info("[LinkedIn] Login successful âœ“")
            # Save fresh cookies so the next run can inject and skip the form.
            try:
                raw = await page.context.cookies("https://www.linkedin.com")
                fresh = {c["name"]: c["value"] for c in raw}
                payload = {
                    "saved_at": time.time(),
                    "username": self._linkedin_identity,
                    "cookies": fresh,
                }
                self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
                self._cookie_path.write_text(json.dumps(payload))
                if fresh.get("li_at"):
                    legacy_path = legacy_linkedin_cookie_path()
                    legacy_path.parent.mkdir(parents=True, exist_ok=True)
                    legacy_path.write_text(json.dumps(payload))
                logger.info("[LinkedIn] Fresh session cookies saved to disk")
            except Exception as exc:
                logger.warning(f"[LinkedIn] Could not save cookies: {exc}")
        else:
            await self._bm.screenshot(page, "linkedin_login_failed")
            challenge = await detect_linkedin_auth_challenge(page)
            if challenge:
                self._set_auth_error(challenge.message)
                self._report_progress(f"[LinkedIn][Auth] {challenge.message}", level="warning")
                logger.warning(f"[LinkedIn] Login failed due to auth challenge: {challenge.message}")
                return
            self._set_auth_error(
                f"LinkedIn login did not complete successfully (current URL: {current_url})."
            )
            logger.warning(
                f"[LinkedIn] Login may have failed — currently at: {current_url} "
                "Check data/screenshots/linkedin_login_failed.png"
            )

    # ------------------------------------------------------------------
    # Easy Apply
    # ------------------------------------------------------------------

    async def _easy_apply(
        self,
        job: Job,
        tailored_resume_path: str | None = None,
        cover_letter: str | None = None,
    ) -> ApplicationResult:
        page = self._page
        self._tailored_resume_path = tailored_resume_path
        self._cover_letter_text = cover_letter
        self._report_progress(f"[LinkedIn][Apply] Starting Easy Apply for {job.title} @ {job.company}")
        logger.info(f"[LinkedIn] Easy Apply â†’ {job.title} @ {job.company}")

        # Navigate to job page â€” normalize locale subdomains first (safety net for
        # URLs stored in DB before the scraper fix, e.g. de.linkedin.com â†’ www.linkedin.com)
        job_url = re.sub(r"https://[a-z]{2}\.linkedin\.com/", "https://www.linkedin.com/", job.url)
        self._report_progress(f"[LinkedIn][Apply] Opening job page: {job_url}")
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            return self._fail(job, f"Could not navigate to job page: {exc}")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Detect mid-run session expiry
        if "/login" in page.url or "/authwall" in page.url:
            self._report_progress("[LinkedIn][Auth] Redirected to login mid-run")
            logger.info("[LinkedIn] Redirected to login mid-run â€” re-authenticating â€¦")
            await self._ensure_logged_in()
            if not self._logged_in:
                return self._fail(
                    job,
                    self._auth_error or "LinkedIn authentication unavailable",
                )
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)

        # Already applied to this job?
        if await self._job_already_submitted(page):
            message = "Application already submitted for this job"
            self._report_progress(
                f"[LinkedIn][Apply] Application already submitted for {job.title} @ {job.company}"
            )
            logger.info(f"[LinkedIn] Application already submitted: {job.title} @ {job.company}")
            return self._ok(job, message=message)

        # Dismiss overlay prompts before looking for the apply button
        await self._dismiss_prompts(page)

        easy_btn, generic_btn = await self._wait_for_apply_controls(page)
        if not easy_btn and not generic_btn:
            logger.debug("[LinkedIn] No apply controls found after initial load - reloading once")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.debug(f"[LinkedIn] Reload after missing apply controls failed: {exc}")
            easy_btn, generic_btn = await self._wait_for_apply_controls(page, timeout_ms=6_000)

        if not easy_btn:
            if await self._job_already_submitted(page):
                message = "Application already submitted for this job"
                self._report_progress(
                    f"[LinkedIn][Apply] Application already submitted for {job.title} @ {job.company}"
                )
                logger.info(f"[LinkedIn] Application already submitted: {job.title} @ {job.company}")
                return self._ok(job, message=message)
            # Auth check — if session was lost the page redirects to authwall
            if "/authwall" in page.url or "/login" in page.url:
                return self._fail(
                    job,
                    "Not authenticated — redirected to authwall. "
                    "Check LINKEDIN_EMAIL / LINKEDIN_PASSWORD env vars.",
                )
            # Check for a generic (external) apply button to distinguish skip vs fail
            if generic_btn:
                label = (
                    f"{await generic_btn.get_attribute('aria-label') or ''} "
                    f"{await generic_btn.inner_text() or ''}"
                ).lower()
                if "easy apply" not in label:
                    return self._skip(job, "No Easy Apply button — external apply only")
            await self._bm.screenshot(page, f"no_apply_btn_{job.external_id}")
            return self._fail(
                job, "No apply button found — job may have expired or been filled"
            )

        btn = easy_btn
        try:
            await btn.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
        except Exception:
            pass
        await btn.click()

        if await self._job_search_safety_reminder_visible(page):
            message = "LinkedIn triggered Job Search Safety Reminder for this job. Skipping it."
            self._report_progress(
                f"[LinkedIn][Apply] LinkedIn triggered Job Search Safety Reminder for {job.title} @ {job.company}. Skipping it.",
                level="warning",
            )
            logger.warning(f"[LinkedIn] Job Search Safety Reminder shown for {job.title} @ {job.company}")
            await self._bm.screenshot(page, f"safety_reminder_{job.external_id}")
            return self._skip(job, message)

        try:
            await page.wait_for_selector(_MODAL, timeout=25_000)
        except PWTimeoutError:
            if await self._job_search_safety_reminder_visible(page):
                message = "LinkedIn triggered Job Search Safety Reminder for this job. Skipping it."
                self._report_progress(
                    f"[LinkedIn][Apply] LinkedIn triggered Job Search Safety Reminder for {job.title} @ {job.company}. Skipping it.",
                    level="warning",
                )
                logger.warning(
                    f"[LinkedIn] Job Search Safety Reminder shown for {job.title} @ {job.company}"
                )
                await self._bm.screenshot(page, f"safety_reminder_{job.external_id}")
                return self._skip(job, message)
            try:
                modal = page.locator(_MODAL).first
                if await modal.count() == 0 or not await modal.is_visible():
                    await self._bm.screenshot(page, f"modal_timeout_{job.external_id}")
                    return self._fail(job, "Easy Apply modal did not open (timeout)")
                logger.debug("[LinkedIn] Easy Apply modal became visible after slow open")
            except Exception:
                await self._bm.screenshot(page, f"modal_timeout_{job.external_id}")
                return self._fail(job, "Easy Apply modal did not open (timeout)")
        self._report_progress("[LinkedIn][Apply] Easy Apply modal opened")
        logger.debug("[LinkedIn] Easy Apply modal opened")

        # Step through the multi-page modal
        max_steps = 15
        prev_step_label = ""
        stuck_count = 0

        for step in range(max_steps):
            await asyncio.sleep(random.uniform(0.6, 1.0))

            step_label = await self._get_step_label(page)
            self._report_progress(
                f"[LinkedIn][Step] {step + 1}{': ' + step_label if step_label else ''}"
            )
            logger.info(f"[LinkedIn] Modal step {step + 1}{': ' + step_label if step_label else ''}")

            # Final step: submit button is now visible
            submit = await self._find_submit_button(page)
            if submit:
                await self._bm.screenshot(page, f"pre_submit_{job.external_id}")
                self._report_progress("[LinkedIn][Submit] Submitting application")
                await self._click_modal_button(page, submit, "submit")
                await asyncio.sleep(2)
                await self._bm.screenshot(page, f"post_submit_{job.external_id}")
                logger.info(f"[LinkedIn] âœ“ Application submitted: {job.title} @ {job.company}")
                self._report_progress(
                    f"[LinkedIn][Submit] Application submitted for {job.title} @ {job.company}"
                )
                return self._ok(job)

            # Fill all visible fields on this modal page
            await self._fill_modal_page(page, job)

            # Check for validation errors before advancing
            error_el = page.locator(_ERROR_MSG).first
            if await error_el.count() > 0:
                error_text = await error_el.inner_text()
                self._report_progress(
                    f"[LinkedIn][Validation] Step {step + 1} error: {error_text}",
                    level="warning",
                )
                logger.warning(f"[LinkedIn] Validation error at step {step + 1}: {error_text}")
                await self._bm.screenshot(page, f"form_error_{job.external_id}_{step}")
                return self._fail(job, f"Form validation error: {error_text}")

            # Stuck detection â€” same step label twice in a row
            if step_label and step_label == prev_step_label:
                stuck_count += 1
                if stuck_count >= 2:
                    await self._bm.screenshot(page, f"stuck_{job.external_id}_{step}")
                    return self._fail(job, f"Stuck on modal step: '{step_label}'")
            else:
                stuck_count = 0
            prev_step_label = step_label

            # Advance to next step
            next_btn = await self._find_next_button(page)
            if next_btn and await next_btn.is_enabled():
                self._report_progress("[LinkedIn][Step] Advancing to the next step")
                await self._click_modal_button(page, next_btn, "next step")
                # Give the modal animation time to transition before the next fill pass
                await asyncio.sleep(0.5)
            else:
                self._report_progress(
                    f"[LinkedIn][Step] Could not advance past step {step + 1}",
                    level="warning",
                )
                logger.warning(f"[LinkedIn] No Next/Submit button at step {step + 1}")
                await self._bm.screenshot(page, f"no_next_{job.external_id}_{step}")
                return self._fail(job, f"Stuck at modal step {step + 1} â€” no Next button")

        return self._fail(job, "Exceeded max modal steps without submitting")

    async def _dismiss_prompts(self, page: Page) -> None:
        """Dismiss overlay prompts (Follow, notifications) that cover the apply button."""
        for selector in _FOLLOW_PROMPT_DISMISS.split(", "):
            try:
                el = page.locator(selector.strip()).first
                if await el.count() > 0 and await el.is_visible():
                    is_apply_modal_close = await el.evaluate(
                        """(node) => {
                            const applyModal = node.closest(
                                'div.jobs-easy-apply-modal, div.jobs-apply-modal'
                            );
                            if (applyModal) {
                                return true;
                            }
                            const dialog = node.closest('div[role="dialog"]');
                            if (!dialog) {
                                return false;
                            }
                            return /save this application/i.test(dialog.innerText || '');
                        }"""
                    )
                    if is_apply_modal_close:
                        continue
                    await el.click(timeout=2_000, force=True)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _get_step_label(self, page: Page) -> str:
        """Return the current step indicator text, e.g. 'Step 2 of 4', or empty string."""
        try:
            el = page.locator(_STEP_INDICATOR).first
            if await el.count() > 0:
                return (await el.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def _job_already_submitted(self, page: Page) -> bool:
        try:
            indicator = page.locator(_ALREADY_APPLIED)
            if await indicator.count() > 0 and await indicator.first.is_visible():
                return True
        except Exception:
            pass

        candidates = [
            page.get_by_text(_ALREADY_APPLIED_TEXT),
            page.locator("text=/Application submitted/i"),
        ]
        return await self._first_visible(candidates) is not None

    async def _job_search_safety_reminder_visible(self, page: Page) -> bool:
        candidates = [
            page.get_by_text(_JOB_SEARCH_SAFETY_REMINDER_TEXT),
            page.locator("text=/Job search safety reminder/i"),
        ]
        return await self._first_visible(candidates) is not None

    async def _first_visible(self, candidates: list[Locator]) -> Locator | None:
        for candidate in candidates:
            try:
                if await candidate.count() > 0 and await candidate.first.is_visible():
                    return candidate.first
            except Exception:
                continue
        return None

    async def _wait_for_apply_controls(
        self,
        page: Page,
        timeout_ms: int = 8_000,
    ) -> tuple[Locator | None, Locator | None]:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            easy_btn = await self._first_visible([
                page.get_by_role("button", name=re.compile(r"easy\s+apply", re.I)),
                page.locator("button").filter(has_text=re.compile(r"easy\s+apply", re.I)),
                page.locator("a").filter(has_text=re.compile(r"easy\s+apply", re.I)),
                page.locator(_EASY_APPLY_BTN),
            ])
            if easy_btn:
                return easy_btn, None

            generic_btn = await self._first_visible([
                page.get_by_role("button", name=re.compile(r"\bapply\b", re.I)),
                page.locator("button").filter(has_text=re.compile(r"\bapply\b", re.I)),
                page.locator("a").filter(has_text=re.compile(r"\bapply\b", re.I)),
                page.locator(_APPLY_BTN),
            ])
            if generic_btn:
                return None, generic_btn

            await asyncio.sleep(0.4)

        return None, None

    async def _find_submit_button(self, page: Page) -> Locator | None:
        modal = page.locator(_MODAL)
        return await self._first_visible([
            modal.get_by_role("button", name=re.compile(r"submit application|submit", re.I)),
            modal.locator("button").filter(has_text=re.compile(r"submit application|submit", re.I)),
            modal.locator(_SUBMIT_BTN),
        ])

    async def _find_next_button(self, page: Page) -> Locator | None:
        modal = page.locator(_MODAL)
        return await self._first_visible([
            modal.get_by_role("button", name=re.compile(r"continue|next|review", re.I)),
            modal.locator("button").filter(has_text=re.compile(r"continue|next|review", re.I)),
            modal.locator(_NEXT_BTN),
        ])

    def _prepare_text_input_value(
        self,
        label: str,
        value: str,
        *,
        field_type: str,
        input_type: str,
        job: Job,
    ) -> str | None:
        cleaned_value = self._clean_answer(value)
        if cleaned_value is None:
            return None
        normalized_label = normalize_question_text(label).lower()
        if any(token in normalized_label for token in ("location", "city", "town")):
            if cleaned_value.strip().lower() in {"not specified", "unknown", "none", "null"}:
                return self._clean_answer(self._infer_value_from_label(label))
        if field_type != "number" and input_type not in {"number", "range"}:
            return cleaned_value

        numeric_value = self._extract_numeric_text(cleaned_value)
        if numeric_value is None:
            numeric_value = self._infer_numeric_value_from_label(label, job)
        return numeric_value

    @staticmethod
    def _extract_numeric_text(value: str) -> str | None:
        normalized = value.replace(",", "").strip()
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if not match:
            return None
        return match.group(0)

    def _infer_numeric_value_from_label(self, label: str, job: Job) -> str | None:
        normalized_label = normalize_question_text(label).lower()

        if any(
            phrase in normalized_label
            for phrase in (
                "years of experience",
                "years experience",
                "how many years",
            )
        ):
            if self.profile.years_of_experience is not None:
                return str(self.profile.years_of_experience)

        if any(
            phrase in normalized_label
            for phrase in (
                "salary expectation",
                "salary expected",
                "expected salary",
                "salary requirement",
                "compensation expectation",
                "desired compensation",
                "desired salary",
                "annual pay",
                "expected pay",
            )
        ):
            numeric_candidates = [
                float(candidate)
                for candidate in (job.salary_min, job.salary_max)
                if candidate is not None and float(candidate) > 0
            ]
            if numeric_candidates:
                if len(numeric_candidates) == 2:
                    return str(int(round(sum(numeric_candidates) / 2)))
                return str(int(round(numeric_candidates[0])))

        return None

    async def _finalize_text_input(self, page: Page, inp: Locator) -> None:
        try:
            await inp.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (typeof el.blur === 'function') {
                        el.blur();
                    }
                }"""
            )
        except Exception:
            pass

        if await self._autocomplete_overlay_visible(page):
            try:
                await self._select_autocomplete_suggestion(page)
            except Exception:
                pass

        await self._dismiss_autocomplete_overlays(page)

    async def _autocomplete_overlay_visible(self, page: Page) -> bool:
        for selector in _AUTOCOMPLETE_RESULTS.split(", "):
            try:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _select_autocomplete_suggestion(self, page: Page) -> bool:
        for selector in _AUTOCOMPLETE_RESULTS.split(", "):
            try:
                options = page.locator(selector)
                if await options.count() == 0 or not await options.first.is_visible():
                    continue
                await options.first.click(timeout=2_000, force=True)
                await asyncio.sleep(0.2)
                return True
            except Exception:
                continue
        return False

    async def _dismiss_autocomplete_overlays(self, page: Page) -> None:
        if not await self._autocomplete_overlay_visible(page):
            return
        if await self._select_autocomplete_suggestion(page):
            return
        try:
            await page.evaluate(
                """() => {
                    const active = document.activeElement;
                    if (active instanceof HTMLElement) {
                        active.blur();
                    }
                }"""
            )
        except Exception:
            pass

    async def _dismiss_save_application_prompt(self, page: Page) -> bool:
        try:
            dialog = page.locator("div[role='dialog']").filter(
                has_text=re.compile(r"save this application", re.IGNORECASE)
            ).first
            if await dialog.count() == 0 or not await dialog.is_visible():
                return False

            candidates = [
                dialog.locator("button[aria-label='Dismiss']").first,
                dialog.locator("button[aria-label='Close']").first,
                dialog.locator("button.artdeco-modal__dismiss").first,
            ]
            close_button = await self._first_visible(candidates)
            if close_button is None:
                return False

            await close_button.click(timeout=2_000, force=True)
            await asyncio.sleep(0.3)
            still_visible = await dialog.count() > 0 and await dialog.is_visible()
            if not still_visible:
                logger.debug("[LinkedIn] Closed save-application confirmation dialog")
                return True
        except Exception as exc:
            logger.debug(
                "[LinkedIn] Save-application confirmation dismissal failed: "
                f"{self._summarize_exception(exc)}"
            )
        return False

    async def _click_modal_button(self, page: Page, button: Locator, action_name: str) -> None:
        await self._dismiss_prompts(page)
        await self._dismiss_save_application_prompt(page)
        await self._dismiss_autocomplete_overlays(page)
        try:
            await button.scroll_into_view_if_needed()
        except Exception:
            pass

        for description, click in (
            ("native click", lambda: button.click(timeout=2_000)),
            ("forced click", lambda: button.click(timeout=2_000, force=True)),
        ):
            try:
                await click()
                return
            except Exception as exc:
                logger.debug(
                    f"[LinkedIn] {action_name.title()} button {description} failed: "
                    f"{self._summarize_exception(exc)}"
                )
                await self._dismiss_save_application_prompt(page)
                await self._dismiss_autocomplete_overlays(page)

        try:
            await button.dispatch_event("click", timeout=2_000)
            return
        except Exception as exc:
            logger.debug(
                f"[LinkedIn] {action_name.title()} button event dispatch failed: "
                f"{self._summarize_exception(exc)}"
            )

        raise RuntimeError(f"Could not click {action_name} button")

    @staticmethod
    def _modal_descendants(selector: str) -> str:
        modal_roots = [part.strip() for part in _MODAL.split(",")]
        return ", ".join(f"{root} {selector}" for root in modal_roots)

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def _fill_modal_page(self, page: Page, job: Job) -> None:
        """Fill all visible form fields on the current modal page."""
        await self._fill_text_inputs(page, job)
        await self._fill_textareas(page, job)
        await self._fill_selects(page)
        await self._fill_radio_buttons(page)
        await self._fill_checkboxes(page)
        await self._handle_resume_upload(page)
        await self._dismiss_autocomplete_overlays(page)

    async def _fill_text_inputs(self, page: Page, job: Job) -> None:
        inputs = await page.locator(self._modal_descendants(_TEXT_INPUTS)).all()
        for inp in inputs:
            if not await inp.is_visible() or not await inp.is_enabled():
                continue
            label = await self._get_field_label(page, inp)
            input_type = ((await inp.get_attribute("type")) or "text").strip().lower()
            field_type = "number" if input_type in {"number", "range"} else "text"
            current_val = await inp.input_value()
            if current_val.strip():
                override_value = self._get_prefill_override_value(
                    label,
                    current_val,
                    field_type=field_type,
                )
                if override_value is not None:
                    prepared_override = self._prepare_text_input_value(
                        label,
                        override_value,
                        field_type=field_type,
                        input_type=input_type,
                        job=job,
                    )
                    if prepared_override is not None:
                        await inp.fill("")
                        await _BM.human_type(inp, prepared_override)
                        await self._finalize_text_input(page, inp)
                        self._remember_answer(label, prepared_override)
                        self._record_answer(label, prepared_override, "profile", field_type)
                        continue
                if label:
                    self._record_prefilled_answer(label, current_val, field_type)
                continue  # already filled

            value, source = await self._resolve_answer(label, field_type)
            if value is not None and source is not None:
                prepared_value = self._prepare_text_input_value(
                    label,
                    value,
                    field_type=field_type,
                    input_type=input_type,
                    job=job,
                )
                if prepared_value is None:
                    if label:
                        self._mark_unanswered(label, field_type)
                    continue
                await inp.fill("")
                await _BM.human_type(inp, prepared_value)
                await self._finalize_text_input(page, inp)
                self._record_answer(label, prepared_value, source, field_type)
            elif label:
                self._mark_unanswered(label, field_type)

    async def _fill_textareas(self, page: Page, job: Job) -> None:
        areas = await page.locator(self._modal_descendants(_TEXTAREAS)).all()
        for area in areas:
            if not await area.is_visible() or not await area.is_enabled():
                continue
            label = await self._get_field_label(page, area)
            current_val = await area.input_value()
            if current_val.strip():
                if label:
                    self._record_prefilled_answer(label, current_val, "textarea")
                continue

            label_lower = label.lower()

            if "cover letter" in label_lower or "cover_letter" in label_lower:
                cl_text = getattr(self, "_cover_letter_text", None)
                cl = cl_text if cl_text else self._build_cover_letter(job)
                await area.fill(cl)
                self._record_answer(label or "Cover letter", cl, "generated", "textarea")
                continue

            if "additional" in label_lower or "summary" in label_lower or "about" in label_lower:
                if self.profile.summary:
                    await area.fill(self.profile.summary)
                    self._remember_answer(label, self.profile.summary)
                    self._record_answer(label, self.profile.summary, "inferred", "textarea")
                    continue

            value, source = await self._resolve_answer(label, "textarea")
            if value is not None and source is not None:
                await area.fill(value)
                self._record_answer(label, value, source, "textarea")
            elif label:
                self._mark_unanswered(label, "textarea")

    async def _fill_selects(self, page: Page) -> None:
        selects = await page.locator(self._modal_descendants(_SELECTS)).all()
        for sel in selects:
            if not await sel.is_visible() or not await sel.is_enabled():
                continue
            label = await self._get_field_label(page, sel)
            options = []
            option_elements = await sel.locator("option").all()
            for option in option_elements:
                option_label = (await option.inner_text()).strip()
                option_value = (await option.get_attribute("value") or "").strip()
                candidate = option_label or option_value
                if candidate and candidate.lower() != "select an option":
                    options.append(candidate)

            current_val = await sel.input_value()
            if current_val and current_val not in ("", "Select an option"):
                selected_label = current_val
                try:
                    selected_option = sel.locator("option:checked").first
                    if await selected_option.count() > 0:
                        candidate = self._clean_answer(await selected_option.inner_text())
                        if candidate is not None:
                            selected_label = candidate
                except Exception:
                    pass
                override_value = self._get_prefill_override_value(
                    label,
                    selected_label,
                    field_type="select",
                    options=options,
                )
                if override_value is not None:
                    try:
                        await sel.select_option(value=str(override_value))
                    except Exception:
                        try:
                            await sel.select_option(label=str(override_value))
                        except Exception:
                            logger.debug(
                                f"[LinkedIn] Could not override '{label}' to '{override_value}'"
                            )
                        else:
                            self._remember_answer(label, override_value)
                            self._record_answer(label, override_value, "profile", "select")
                            continue
                    else:
                        self._remember_answer(label, override_value)
                        self._record_answer(label, override_value, "profile", "select")
                        continue
                if label:
                    self._record_prefilled_answer(label, selected_label, "select")
                continue

            value, source = await self._resolve_answer(label, "select", options=options)
            if value is not None and source is not None:
                try:
                    await sel.select_option(value=str(value))
                except Exception:
                    try:
                        await sel.select_option(label=str(value))
                    except Exception:
                        logger.debug(f"[LinkedIn] Could not select '{value}' for '{label}'")
                        self._mark_unanswered(label, "select", options=options)
                        continue
                self._record_answer(label, value, source, "select")
            elif label:
                self._mark_unanswered(label, "select", options=options)

    def _get_prefill_override_value(
        self,
        label: str,
        current_value: str,
        *,
        field_type: str,
        options: list[str] | None = None,
    ) -> str | None:
        normalized_label = normalize_question_text(label).lower()
        if not normalized_label:
            return None
        if not any(
            token in normalized_label
            for token in (
                "first name",
                "given name",
                "last name",
                "surname",
                "family name",
                "email",
                "phone",
                "mobile",
                "location",
                "city",
                "state",
                "province",
                "zip",
                "postal",
                "country code",
                "country",
            )
        ):
            return None

        expected = self._clean_answer(self._infer_value_from_label(label))
        current = self._clean_answer(current_value)
        if expected is None or current is None:
            return None

        override_value = expected
        if field_type == "select":
            override_value = self._match_answer_to_options(expected, options or [])
            if override_value is None:
                return None

        if self._normalize_choice(current) == self._normalize_choice(override_value):
            return None
        return override_value

    async def _fill_radio_buttons(self, page: Page) -> None:
        fieldsets = await page.locator(self._modal_descendants(_RADIO_GROUPS)).all()
        for fieldset in fieldsets:
            if not await fieldset.is_visible():
                continue

            legend = ""
            if await fieldset.locator("legend").count() > 0:
                legend = normalize_question_text(
                    (await fieldset.locator("legend").first.inner_text()).strip()
                )

            radios = await fieldset.locator("input[type='radio']").all()
            radio_options: list[tuple[Locator, str]] = []
            for radio in radios:
                radio_id = await radio.get_attribute("id") or ""
                radio_label_el = page.locator(f"label[for='{radio_id}']")
                radio_label = ""
                if await radio_label_el.count() > 0:
                    radio_label = normalize_question_text(
                        (await radio_label_el.inner_text()).strip()
                    )
                radio_value = (await radio.get_attribute("value") or "").strip()
                radio_options.append((radio, radio_label or radio_value))

            prefilled_option = None
            for radio, option in radio_options:
                try:
                    if await radio.is_checked():
                        prefilled_option = option or (await radio.get_attribute("value") or "").strip()
                        break
                except Exception:
                    continue
            if prefilled_option:
                if legend:
                    self._record_prefilled_answer(legend, prefilled_option, "radio")
                continue

            answer, source = await self._resolve_answer(
                legend,
                "radio",
                options=[option for _, option in radio_options if option],
            )
            if answer is None or source is None:
                if legend:
                    self._mark_unanswered(
                        legend,
                        "radio",
                        options=[option for _, option in radio_options if option],
                    )
                continue

            matched_radio = False
            for radio, option in radio_options:
                radio_value = (await radio.get_attribute("value") or "").strip()
                if option == answer or radio_value == answer:
                    radio_id = await radio.get_attribute("id") or ""
                    label_el = page.locator(f"label[for='{radio_id}']") if radio_id else None
                    await self._set_radio_state(
                        radio,
                        label=legend,
                        option_label=option or radio_value,
                        label_el=label_el,
                    )
                    self._record_answer(legend, answer, source, "radio")
                    matched_radio = True
                    break
            if not matched_radio and legend:
                self._mark_unanswered(
                    legend,
                    "radio",
                    options=[option for _, option in radio_options if option],
                )

    async def _fill_checkboxes(self, page: Page) -> None:
        checkboxes = await page.locator(self._modal_descendants(_CHECKBOXES)).all()
        for cb in checkboxes:
            if not await cb.is_visible():
                continue
            cb_id = await cb.get_attribute("id") or ""
            label_el = page.locator(f"label[for='{cb_id}']") if cb_id else None
            label = ""
            if label_el is not None and await label_el.count() > 0:
                label = (await label_el.inner_text()).strip()

            try:
                if await cb.is_checked():
                    if label:
                        self._record_prefilled_answer(label, "Yes", "checkbox")
                    continue
            except Exception:
                pass

            answer, source = await self._resolve_answer(label, "checkbox", options=["Yes", "No"])
            if answer is None or source is None:
                if label:
                    self._mark_unanswered(label, "checkbox")
                continue

            should_check = str(answer).lower() in ("yes", "true", "1", "checked")
            await self._set_checkbox_state(cb, should_check, label=label, label_el=label_el)
            self._record_answer(label, "Yes" if should_check else "No", source, "checkbox")

    async def _set_checkbox_state(
        self,
        checkbox: Locator,
        should_check: bool,
        label: str = "",
        label_el: Locator | None = None,
    ) -> None:
        """Set checkbox state with fallbacks for covered/custom-styled inputs."""
        current = await checkbox.is_checked()
        if current == should_check:
            return

        action_name = "check" if should_check else "uncheck"
        input_name = await checkbox.get_attribute("name") or await checkbox.get_attribute("id") or ""
        target_name = label or input_name or "checkbox"

        async def _native_toggle(*, force: bool = False) -> None:
            if should_check:
                await checkbox.check(timeout=2_000, force=force)
            else:
                await checkbox.uncheck(timeout=2_000, force=force)

        for description, action in (
            ("native toggle", lambda: _native_toggle(force=False)),
            ("forced toggle", lambda: _native_toggle(force=True)),
        ):
            try:
                await action()
                if await checkbox.is_checked() == should_check:
                    return
            except Exception as exc:
                logger.debug(
                    f"[LinkedIn] Checkbox '{target_name}' {description} failed: "
                    f"{self._summarize_exception(exc)}"
                )

        if label_el is not None:
            try:
                await label_el.first.click(timeout=2_000, force=True)
                if await checkbox.is_checked() == should_check:
                    return
            except Exception as exc:
                logger.debug(
                    f"[LinkedIn] Checkbox '{target_name}' label click failed: "
                    f"{self._summarize_exception(exc)}"
                )

        try:
            await checkbox.evaluate(
                """(el, checked) => {
                    el.scrollIntoView({ block: 'center', inline: 'nearest' });
                    if (el.checked !== checked) {
                        el.checked = checked;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                should_check,
            )
            if await checkbox.is_checked() == should_check:
                logger.debug(f"[LinkedIn] Checkbox '{target_name}' set via DOM fallback")
                return
        except Exception as exc:
            logger.debug(
                f"[LinkedIn] Checkbox '{target_name}' DOM fallback failed: "
                f"{self._summarize_exception(exc)}"
            )

        raise RuntimeError(f"Could not {action_name} checkbox '{target_name}'")

    async def _set_radio_state(
        self,
        radio: Locator,
        label: str = "",
        option_label: str = "",
        label_el: Locator | None = None,
    ) -> None:
        """Select a radio option with fallbacks for overlaid/custom-styled inputs."""
        if await radio.is_checked():
            return

        input_name = await radio.get_attribute("name") or await radio.get_attribute("id") or ""
        target_name = label or option_label or input_name or "radio"

        try:
            await radio.check(timeout=2_000)
            if await radio.is_checked():
                return
        except Exception as exc:
            logger.debug(
                f"[LinkedIn] Radio '{target_name}' native toggle failed: "
                f"{self._summarize_exception(exc)}"
            )

        if label_el is not None:
            try:
                await label_el.first.click(timeout=2_000, force=True)
                if await radio.is_checked():
                    return
            except Exception as exc:
                logger.debug(
                    f"[LinkedIn] Radio '{target_name}' label click failed: "
                    f"{self._summarize_exception(exc)}"
                )

        try:
            await radio.check(timeout=2_000, force=True)
            if await radio.is_checked():
                return
        except Exception as exc:
            logger.debug(
                f"[LinkedIn] Radio '{target_name}' forced toggle failed: "
                f"{self._summarize_exception(exc)}"
            )

        try:
            await radio.evaluate(
                """(el) => {
                    el.scrollIntoView({ block: 'center', inline: 'nearest' });
                    if (!el.checked) {
                        el.checked = true;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }"""
            )
            if await radio.is_checked():
                logger.debug(f"[LinkedIn] Radio '{target_name}' set via DOM fallback")
                return
        except Exception as exc:
            logger.debug(
                f"[LinkedIn] Radio '{target_name}' DOM fallback failed: "
                f"{self._summarize_exception(exc)}"
            )

        raise RuntimeError(f"Could not select radio '{target_name}'")

    async def _handle_resume_upload(self, page: Page) -> None:
        """Upload resume â€” prefer tailored PDF, fall back to profile resume."""
        tailored = getattr(self, "_tailored_resume_path", None)
        if tailored and Path(tailored).exists():
            resume_path = Path(tailored)
            self._report_progress(f"[LinkedIn][Resume] Using tailored resume: {resume_path.name}")
            logger.info(f"[LinkedIn] Using tailored resume: {resume_path.name}")
        else:
            resume_path = None
            if self.profile.resume_path:
                resume_path = Path(self.profile.resume_path)
            elif settings.resume_path:
                resume_path = Path(settings.resume_path)

        if not resume_path or not resume_path.exists():
            logger.debug("[LinkedIn] No resume file found, skipping upload")
            return

        file_input = await self._find_resume_upload_input(page)
        if file_input is None:
            return

        await file_input.set_input_files(str(resume_path))
        self._report_progress(f"[LinkedIn][Resume] Uploaded resume: {resume_path.name}")
        logger.info(f"[LinkedIn] Uploaded resume: {resume_path.name}")
        try:
            await page.wait_for_selector(self._modal_descendants(_RESUME_CARD), timeout=8_000)
        except PWTimeoutError:
            logger.debug("[LinkedIn] Resume upload progress indicator not detected (may be fine)")
        await asyncio.sleep(0.5)

    async def _find_resume_upload_input(self, page: Page) -> Locator | None:
        inputs = await page.locator(self._modal_descendants(_FILE_INPUT)).all()
        if not inputs:
            return None

        ambiguous_candidates: list[Locator] = []
        for file_input in inputs:
            try:
                if not await file_input.is_visible() or not await file_input.is_enabled():
                    continue
            except Exception:
                continue

            label, accept, context = await self._describe_upload_field(page, file_input)
            target_name = label or "file upload"
            if self._is_non_resume_upload_field(accept, context):
                self._report_progress(
                    f"[LinkedIn][Resume] Skipping non-resume upload field: {target_name}"
                )
                continue
            if self._is_resume_upload_field(accept, context):
                return file_input
            ambiguous_candidates.append(file_input)

        if len(ambiguous_candidates) == 1:
            logger.debug("[LinkedIn] Using fallback resume upload input with no explicit label")
            return ambiguous_candidates[0]

        if len(ambiguous_candidates) > 1:
            logger.debug("[LinkedIn] Multiple ambiguous file inputs detected, skipping upload")
        return None

    async def _describe_upload_field(self, page: Page, file_input: Locator) -> tuple[str, str, str]:
        label = normalize_question_text(await self._get_field_label(page, file_input))
        accept = ((await file_input.get_attribute("accept")) or "").strip().lower()
        name = ((await file_input.get_attribute("name")) or "").strip().lower()
        aria_label = normalize_question_text((await file_input.get_attribute("aria-label")) or "")
        nearby_text = ""
        try:
            nearby_text = await file_input.evaluate(
                """(el) => {
                    let node = el.parentElement;
                    for (let depth = 0; depth < 4 && node; depth += 1, node = node.parentElement) {
                        const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (text) {
                            return text.slice(0, 240);
                        }
                    }
                    return '';
                }"""
            )
        except Exception:
            nearby_text = ""

        context = " ".join(
            part
            for part in (
                label.lower(),
                accept,
                name,
                aria_label.lower(),
                normalize_question_text(nearby_text).lower(),
            )
            if part
        )
        return label, accept, context

    @staticmethod
    def _is_resume_upload_field(accept: str, context: str) -> bool:
        accepts_documents = any(
            token in accept
            for token in (
                "application/pdf",
                ".pdf",
                "msword",
                "officedocument",
                ".doc",
                ".docx",
                "wordprocessingml",
            )
        )
        return accepts_documents or any(hint in context for hint in _DOCUMENT_UPLOAD_HINTS)

    @staticmethod
    def _is_non_resume_upload_field(accept: str, context: str) -> bool:
        accepts_documents = LinkedInApplier._is_resume_upload_field(accept, context)
        accepts_images = any(
            token in accept
            for token in (
                "image/",
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                "jpg",
                "jpeg",
                "png",
                "gif",
            )
        )
        if any(hint in context for hint in _IMAGE_UPLOAD_HINTS) and not accepts_documents:
            return True
        return accepts_images and not accepts_documents

    # ------------------------------------------------------------------
    # Label / answer helpers
    # ------------------------------------------------------------------

    async def _get_field_label(self, page: Page, element) -> str:  # type: ignore[no-untyped-def]
        """Best-effort: find the label associated with a form element."""
        el_id = await element.get_attribute("id") or ""
        el_name = await element.get_attribute("name") or ""
        placeholder = await element.get_attribute("placeholder") or ""

        if el_id:
            label_el = page.locator(f"label[for='{el_id}']")
            if await label_el.count() > 0:
                return (await label_el.first.inner_text()).strip()

        # Walk up to enclosing div and look for a label sibling
        try:
            parent_label = await element.evaluate(
                "(el) => {"
                "  let p = el.parentElement;"
                "  for (let i = 0; i < 4; i++) {"
                "    if (!p) break;"
                "    const lbl = p.querySelector('label');"
                "    if (lbl) return lbl.innerText.trim();"
                "    p = p.parentElement;"
                "  }"
                "  return '';"
                "}"
            )
            if parent_label:
                return parent_label
        except Exception:
            pass

        return placeholder or el_name

    def _answer_for_label(self, label: str) -> str | None:
        """Look up the label against user profile custom_answers (case-insensitive)."""
        label_lower = normalize_question_key(normalize_question_text(label))
        if not label_lower:
            return None
        for key, val in self.profile.custom_answers.items():
            normalized_key = normalize_question_key(normalize_question_text(key))
            if normalized_key and (
                normalized_key in label_lower or label_lower in normalized_key
            ):
                return val
        return None

    def _infer_value_from_label(self, label: str) -> str | None:
        """Infer an answer from user profile fields based on label text."""
        label_lower = normalize_question_text(label).lower()
        p = self.profile
        preferred_work_modes = {
            mode.lower()
            for mode in getattr(p.preferences, "preferred_work_modes", [])
            if isinstance(mode, str)
        }

        if any(w in label_lower for w in ("first name", "given name")):
            return p.first_name
        if any(w in label_lower for w in ("last name", "surname", "family name")):
            return p.last_name
        if "email" in label_lower:
            return p.email
        if "country code" in label_lower and "phone" in label_lower:
            return p.address.country if p.address else ""
        if "phone" in label_lower or "mobile" in label_lower:
            return p.phone or ""
        if "linkedin" in label_lower:
            return p.social_links.linkedin or ""
        if "github" in label_lower:
            return p.social_links.github or ""
        if "portfolio" in label_lower or "website" in label_lower:
            return p.social_links.portfolio or ""
        if "city" in label_lower:
            return p.address.city if p.address else ""
        if "state" in label_lower or "province" in label_lower:
            return p.address.state if p.address else ""
        if "zip" in label_lower or "postal" in label_lower:
            return p.address.zip_code if p.address else ""
        if "country" in label_lower:
            return p.address.country if p.address else "US"
        if "years of experience" in label_lower or "years experience" in label_lower:
            return str(p.years_of_experience) if p.years_of_experience else ""
        if "summary" in label_lower or "headline" in label_lower:
            return p.headline or ""
        if any(
            phrase in label_lower
            for phrase in (
                "comfortable commuting",
                "comfortable to commute",
                "able to commute",
                "commuting to this job",
                "commute to this job",
                "commute to the job",
            )
        ):
            return "Yes" if preferred_work_modes & {"hybrid", "onsite"} else "No"
        if any(
            phrase in label_lower
            for phrase in (
                "comfortable working in a hybrid setting",
                "comfortable working in hybrid",
                "hybrid setting",
                "hybrid environment",
            )
        ):
            return "Yes" if "hybrid" in preferred_work_modes else "No"

        return None

    def _build_cover_letter(self, job: Job) -> str:
        """Build a basic cover letter from a template or a sensible default."""
        if self.profile.cover_letter_template_path:
            tmpl_path = Path(self.profile.cover_letter_template_path)
            if tmpl_path.exists():
                tmpl = tmpl_path.read_text()
                return (
                    tmpl
                    .replace("{job_title}", job.title)
                    .replace("{company}", job.company)
                    .replace("{first_name}", self.profile.first_name)
                    .replace("{last_name}", self.profile.last_name)
                )

        return (
            f"Dear Hiring Manager,\n\n"
            f"I am excited to apply for the {job.title} position at {job.company}. "
            f"With {self.profile.years_of_experience or 'several'} years of experience "
            f"in {', '.join(self.profile.skills[:3]) if self.profile.skills else 'software development'}, "
            f"I am confident I can make a strong contribution to your team.\n\n"
            f"Best regards,\n{self.profile.first_name} {self.profile.last_name}"
        )
