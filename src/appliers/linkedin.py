"""LinkedIn applier — Easy Apply automation via Playwright.

Flow
----
1. ``setup()``  — launch Playwright browser; inject warm scraper cookies; verify
                  session or perform fresh login.
2. ``apply()``  — navigate to the job page, click "Easy Apply", step through
                  the modal, and submit.

Login strategy (most → least expensive)
----------------------------------------
1. Load ``data/.linkedin_cookies.json`` (written by the scraper after warming) and
   inject directly into the browser context — cheapest, no form interaction.
2. Navigate to /feed; if session is live, done.
3. If not logged in, perform a full form-based login with human-like delays.
4. Save fresh cookies back to disk so the next run skips login again.

Easy Apply modal steps (order may vary per job):
  • Contact info  — phone, email (usually pre-filled from account)
  • Resume        — select "Use a resume" or upload file
  • Screening Q's — text, number, dropdown, yes/no radio, checkbox
  • Cover letter  — optional textarea
  • Review        — summary page
  • Submit        — final button

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
from src.utils.browser import BrowserManager, BrowserManager as _BM

# ── Cookie / session paths ─────────────────────────────────────────────────────

# Scraper writes warm authenticated cookies here; we reuse them to skip login.
_COOKIE_PATH = Path("data/.linkedin_cookies.json")
_COOKIE_MAX_AGE = 4 * 3600          # reuse cookies up to 4 hours old
_SESSION_DIR   = Path("data/.linkedin_session")  # persistent Playwright profile

# ── URLs ───────────────────────────────────────────────────────────────────────

_LOGIN_URL = "https://www.linkedin.com/login"
_FEED_URL  = "https://www.linkedin.com/feed/"

# ── Login selectors ────────────────────────────────────────────────────────────
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

# ── Job page selectors ─────────────────────────────────────────────────────────

# "Follow" prompt that sometimes blocks the Easy Apply button
_FOLLOW_PROMPT_DISMISS = (
    "button[aria-label='Dismiss'], "
    "button[aria-label='Got it'], "
    "button.artdeco-modal__dismiss"
)

# Job page — aria-label based selectors survive LinkedIn UI/class renames
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

# ── Modal selectors ────────────────────────────────────────────────────────────

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


class LinkedInApplier(BaseApplier):
    """Applies to LinkedIn Easy Apply jobs using Playwright browser automation."""

    board_name = "LinkedIn"
    board_slug = "linkedin"

    def __init__(self, profile: UserProfile):
        super().__init__(profile)
        self._bm: BrowserManager | None = None
        self._page: Page | None = None
        self._logged_in = False
        self._unknown_questions: list[str] = []
        self._answered_questions: list[AnsweredQuestion] = []
        self._learned_answers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        await super().setup()
        # 60 s timeout — LinkedIn pages can be slow; 30 s caused spurious failures.
        self._bm = BrowserManager(
            user_data_dir=_SESSION_DIR,
            timeout_ms=60,          # passed to BrowserManager as seconds → *1000 inside
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

        # Re-verify session before each application — handles long pipelines
        # where cookies expire mid-run.
        if not self._logged_in or not await self._is_logged_in(self._page):
            logger.info("[LinkedIn] Session appears stale — re-authenticating …")
            self._logged_in = False
            await self._ensure_logged_in()

        self._unknown_questions = []
        self._answered_questions = []
        self._learned_answers = {}
        try:
            # Note: we do NOT gate on job.easy_apply here because the DB flag is often
            # wrong (Voyager API scrape sets description but not easy_apply).
            # _easy_apply() navigates to the actual page and checks for the button live —
            # that is the authoritative check. If there's no Easy Apply button, it skips.
            result = await self._easy_apply(
                job,
                tailored_resume_path=tailored_resume_path,
                cover_letter=cover_letter,
            )
        except Exception as exc:
            await self._bm.screenshot(self._page, f"error_{job.external_id}")
            result = self._fail(job, str(exc))

        result.new_questions = list(dict.fromkeys(self._unknown_questions))
        result.answered_questions = list(self._answered_questions)
        result.learned_answers = dict(self._learned_answers)
        self._unknown_questions = []
        self._answered_questions = []
        self._learned_answers = {}
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
    def _normalize_choice(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

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
        return None

    def _remember_answer(self, question: str, answer: str) -> None:
        if not question:
            return
        existing = self.profile.custom_answers.get(question)
        if existing == answer:
            return
        self.profile.custom_answers[question] = answer
        self._learned_answers[question] = answer

    def _record_answer(self, question: str, answer: str, source: str, field_type: str) -> None:
        if not question:
            return
        self._answered_questions.append(
            AnsweredQuestion(
                question=question,
                answer=answer,
                source=source,
                field_type=field_type,
            )
        )
        self._report_progress(
            f"[LinkedIn][Question][{source}] {question} -> {self._preview_answer(answer)}"
        )

    async def _resolve_answer(
        self,
        label: str,
        field_type: str,
        options: list[str] | None = None,
    ) -> tuple[str | None, str | None]:
        question = label.strip()
        if not question:
            return None, None

        choice_options = [option for option in (options or []) if option.strip()]

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

        self._report_progress(
            f"[LinkedIn][Question][ai] Requesting answer for '{question}'",
            level="debug",
        )
        suggested_answers = await resolver(
            [ApplicationQuestionPrompt(question=question, field_type=field_type, options=choice_options)]
        )
        suggested_answer = self._clean_answer(suggested_answers.get(question))
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

    def _mark_unanswered(self, question: str, field_type: str) -> None:
        if not question:
            return
        self._unknown_questions.append(question)
        self._report_progress(
            f"[LinkedIn][Question][unanswered] Could not determine an answer for '{question}' ({field_type})",
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
        if not _COOKIE_PATH.exists():
            logger.debug("[LinkedIn] No scraper cookie file found — will log in fresh")
            return False
        try:
            data = json.loads(_COOKIE_PATH.read_text())
            age = time.time() - data.get("saved_at", 0)
            cookies = data.get("cookies", {})
            if not cookies.get("li_at"):
                logger.debug("[LinkedIn] Scraper cookies present but no li_at — skipping injection")
                return False
            if age > _COOKIE_MAX_AGE:
                self._report_progress(
                    f"[LinkedIn][Auth] Scraper cookies are {age / 3600:.1f}h old - attempting reuse"
                )
            # Convert flat dict → Playwright cookie objects
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
            logger.warning(f"[LinkedIn] Cookie injection failed: {exc}")
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

    async def _ensure_logged_in(self) -> None:
        """
        Establish an authenticated LinkedIn session via the most efficient path:
        1. Inject warm scraper cookies → navigate to feed → done if session is live.
        2. Perform a fresh form-based login with human-like delays.
        3. Save fresh cookies back to disk for the next run.
        """
        page = self._page

        # ── 1. Resolve credentials ──────────────────────────────────────────
        # Priority: profile job_board_accounts → LINKEDIN_EMAIL/PASSWORD env vars
        creds = self.profile.job_board_accounts.linkedin
        username = (creds.username or "").strip() if creds else ""
        password = (creds.password or "").strip() if creds else ""
        if (not username or not password) and settings.linkedin_email:
            username = settings.linkedin_email.strip()
            password = (settings.linkedin_password or "").strip()

        # ── 2. Inject warm scraper cookies ──────────────────────────────────
        await self._inject_scraper_cookies()

        # ── 3. Check if already logged in ───────────────────────────────────
        try:
            await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning(f"[LinkedIn] Feed navigation failed: {exc}")

        if await self._is_logged_in(page):
            self._report_progress("[LinkedIn][Auth] Session active")
            logger.info("[LinkedIn] Session active — already logged in ✓")
            self._logged_in = True
            return

        # ── 4. Fresh login ───────────────────────────────────────────────────
        if not username or not password:
            logger.warning(
                "[LinkedIn] No credentials available. "
                "Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD env vars, "
                "or enter them in the Profile → Job Board Credentials section."
            )
            return

        logger.info(f"[LinkedIn] Logging in as {username} …")
        self._report_progress(f"[LinkedIn][Auth] Logging in as {username}")
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error(f"[LinkedIn] Could not load login page: {exc}")
            return
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # If already redirected away from /login, check whether we're authenticated
        if "/login" not in page.url and "/checkpoint" not in page.url:
            if await self._is_logged_in(page):
                self._report_progress("[LinkedIn][Auth] Persistent profile session detected")
                logger.info("[LinkedIn] Persistent profile session detected — already logged in ✓")
                self._logged_in = True
                return

        # Wait for the standard email input with a short timeout so we detect
        # checkpoint/CAPTCHA pages quickly instead of hanging for 15 s.
        form_visible = False
        try:
            await page.wait_for_selector("input#username", state="visible", timeout=8_000)
            form_visible = True
        except PWTimeoutError:
            # Login form not visible — three possible cases:
            # 1. Persistent profile cookie already authenticated → redirected to /feed (success!)
            # 2. LinkedIn showing checkpoint/CAPTCHA → cannot proceed automatically
            # 3. Network issue or unexpected page state
            # IMPORTANT: never clear_cookies() here — that destroys the valid li_at token
            # that was just injected from the scraper warm cookies.
            if await self._is_logged_in(page):
                logger.info("[LinkedIn] Already logged in (persistent profile active) ✓")
                self._logged_in = True
                return
            if any(s in page.url for s in ("checkpoint", "challenge", "captcha")):
                await self._bm.screenshot(page, "linkedin_login_checkpoint")
                logger.error(
                    "[LinkedIn] LinkedIn is showing a security checkpoint at "
                    f"{page.url}. "
                    "Fix: set HEADLESS_BROWSER=false in your .env, run the pipeline once, "
                    "solve the challenge manually — the session cookie persists for future runs."
                )
                return
            await self._bm.screenshot(page, "linkedin_login_no_form")
            logger.error(
                f"[LinkedIn] Login form not visible at {page.url} — cannot authenticate. "
                "Try running with HEADLESS_BROWSER=false to diagnose."
            )
            return

        if not form_visible:
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
        if "checkpoint" in current_url or "challenge" in current_url:
            await self._bm.screenshot(page, "linkedin_login_checkpoint")
            logger.warning(
                "[LinkedIn] Login requires manual verification (CAPTCHA / 2-FA). "
                "Set HEADLESS_BROWSER=false in your .env, solve the checkpoint once, "
                "then restart — the session cookie will persist across runs."
            )
            return

        if await self._is_logged_in(page):
            self._logged_in = True
            logger.info("[LinkedIn] Login successful ✓")
            # Save fresh cookies so the next run can inject and skip the form.
            try:
                raw = await page.context.cookies("https://www.linkedin.com")
                fresh = {c["name"]: c["value"] for c in raw}
                _COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _COOKIE_PATH.write_text(
                    json.dumps({"saved_at": time.time(), "cookies": fresh})
                )
                logger.info("[LinkedIn] Fresh session cookies saved to disk")
            except Exception as exc:
                logger.warning(f"[LinkedIn] Could not save cookies: {exc}")
        else:
            await self._bm.screenshot(page, "linkedin_login_failed")
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
        logger.info(f"[LinkedIn] Easy Apply → {job.title} @ {job.company}")

        # Navigate to job page — normalize locale subdomains first (safety net for
        # URLs stored in DB before the scraper fix, e.g. de.linkedin.com → www.linkedin.com)
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
            logger.info("[LinkedIn] Redirected to login mid-run — re-authenticating …")
            await self._ensure_logged_in()
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)

        # Already applied to this job?
        if await page.locator(_ALREADY_APPLIED).count() > 0:
            return self._skip(job, "Already applied to this job")

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

        try:
            await page.wait_for_selector(_MODAL, timeout=10_000)
        except PWTimeoutError:
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
                await submit.click()
                await asyncio.sleep(2)
                await self._bm.screenshot(page, f"post_submit_{job.external_id}")
                logger.info(f"[LinkedIn] ✓ Application submitted: {job.title} @ {job.company}")
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

            # Stuck detection — same step label twice in a row
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
                await next_btn.click()
                # Give the modal animation time to transition before the next fill pass
                await asyncio.sleep(0.5)
            else:
                self._report_progress(
                    f"[LinkedIn][Step] Could not advance past step {step + 1}",
                    level="warning",
                )
                logger.warning(f"[LinkedIn] No Next/Submit button at step {step + 1}")
                await self._bm.screenshot(page, f"no_next_{job.external_id}_{step}")
                return self._fail(job, f"Stuck at modal step {step + 1} — no Next button")

        return self._fail(job, "Exceeded max modal steps without submitting")

    async def _dismiss_prompts(self, page: Page) -> None:
        """Dismiss overlay prompts (Follow, notifications) that cover the apply button."""
        for selector in _FOLLOW_PROMPT_DISMISS.split(", "):
            try:
                el = page.locator(selector.strip()).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
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

    @staticmethod
    def _modal_descendants(selector: str) -> str:
        modal_roots = [part.strip() for part in _MODAL.split(",")]
        return ", ".join(f"{root} {selector}" for root in modal_roots)

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def _fill_modal_page(self, page: Page, job: Job) -> None:
        """Fill all visible form fields on the current modal page."""
        await self._fill_text_inputs(page)
        await self._fill_textareas(page, job)
        await self._fill_selects(page)
        await self._fill_radio_buttons(page)
        await self._fill_checkboxes(page)
        await self._handle_resume_upload(page)

    async def _fill_text_inputs(self, page: Page) -> None:
        inputs = await page.locator(self._modal_descendants(_TEXT_INPUTS)).all()
        for inp in inputs:
            if not await inp.is_visible() or not await inp.is_enabled():
                continue
            current_val = await inp.input_value()
            if current_val.strip():
                continue  # already filled

            label = await self._get_field_label(page, inp)
            value, source = await self._resolve_answer(label, "text")
            if value is not None and source is not None:
                await inp.fill("")
                await _BM.human_type(inp, value)
                self._record_answer(label, value, source, "text")
            elif label:
                self._mark_unanswered(label, "text")

    async def _fill_textareas(self, page: Page, job: Job) -> None:
        areas = await page.locator(self._modal_descendants(_TEXTAREAS)).all()
        for area in areas:
            if not await area.is_visible() or not await area.is_enabled():
                continue
            current_val = await area.input_value()
            if current_val.strip():
                continue

            label = await self._get_field_label(page, area)
            label_lower = label.lower()

            if "cover letter" in label_lower or "cover_letter" in label_lower:
                cl_text = getattr(self, "_cover_letter_text", None)
                cl = cl_text if cl_text else self._build_cover_letter(job)
                await area.fill(cl)
                self._report_progress(
                    f"[LinkedIn][Question][generated] {label or 'Cover letter'} -> {self._preview_answer(cl)}"
                )
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
            current_val = await sel.input_value()
            if current_val and current_val not in ("", "Select an option"):
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

            value, source = await self._resolve_answer(label, "select", options=options)
            if value is not None and source is not None:
                try:
                    await sel.select_option(value=str(value))
                except Exception:
                    try:
                        await sel.select_option(label=str(value))
                    except Exception:
                        logger.debug(f"[LinkedIn] Could not select '{value}' for '{label}'")
                        self._mark_unanswered(label, "select")
                        continue
                self._record_answer(label, value, source, "select")
            elif label:
                self._mark_unanswered(label, "select")

    async def _fill_radio_buttons(self, page: Page) -> None:
        fieldsets = await page.locator(self._modal_descendants(_RADIO_GROUPS)).all()
        for fieldset in fieldsets:
            if not await fieldset.is_visible():
                continue

            legend = ""
            if await fieldset.locator("legend").count() > 0:
                legend = (await fieldset.locator("legend").first.inner_text()).strip()

            radios = await fieldset.locator("input[type='radio']").all()
            radio_options: list[tuple[Locator, str]] = []
            for radio in radios:
                radio_id = await radio.get_attribute("id") or ""
                radio_label_el = page.locator(f"label[for='{radio_id}']")
                radio_label = ""
                if await radio_label_el.count() > 0:
                    radio_label = (await radio_label_el.inner_text()).strip()
                radio_value = (await radio.get_attribute("value") or "").strip()
                radio_options.append((radio, radio_label or radio_value))

            answer, source = await self._resolve_answer(
                legend,
                "radio",
                options=[option for _, option in radio_options if option],
            )
            if answer is None or source is None:
                if legend:
                    self._mark_unanswered(legend, "radio")
                continue

            matched_radio = False
            for radio, option in radio_options:
                radio_value = (await radio.get_attribute("value") or "").strip()
                if option == answer or radio_value == answer:
                    await radio.check()
                    self._record_answer(legend, answer, source, "radio")
                    matched_radio = True
                    break
            if not matched_radio and legend:
                self._mark_unanswered(legend, "radio")

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
                logger.debug(f"[LinkedIn] Checkbox '{target_name}' {description} failed: {exc}")

        if label_el is not None:
            try:
                await label_el.first.click(timeout=2_000, force=True)
                if await checkbox.is_checked() == should_check:
                    return
            except Exception as exc:
                logger.debug(f"[LinkedIn] Checkbox '{target_name}' label click failed: {exc}")

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
            logger.debug(f"[LinkedIn] Checkbox '{target_name}' DOM fallback failed: {exc}")

        raise RuntimeError(f"Could not {action_name} checkbox '{target_name}'")

    async def _handle_resume_upload(self, page: Page) -> None:
        """Upload resume — prefer tailored PDF, fall back to profile resume."""
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

        file_input = page.locator(self._modal_descendants(_FILE_INPUT)).first
        if await file_input.count() == 0:
            return

        if not resume_path or not resume_path.exists():
            logger.debug("[LinkedIn] No resume file found, skipping upload")
            return

        await file_input.set_input_files(str(resume_path))
        self._report_progress(f"[LinkedIn][Resume] Uploaded resume: {resume_path.name}")
        logger.info(f"[LinkedIn] Uploaded resume: {resume_path.name}")
        try:
            await page.wait_for_selector(self._modal_descendants(_RESUME_CARD), timeout=8_000)
        except PWTimeoutError:
            logger.debug("[LinkedIn] Resume upload progress indicator not detected (may be fine)")
        await asyncio.sleep(0.5)

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
        label_lower = label.lower().strip()
        for key, val in self.profile.custom_answers.items():
            if key.lower().strip() in label_lower or label_lower in key.lower().strip():
                return val
        return None

    def _infer_value_from_label(self, label: str) -> str | None:
        """Infer an answer from user profile fields based on label text."""
        label_lower = label.lower()
        p = self.profile

        if any(w in label_lower for w in ("first name", "given name")):
            return p.first_name
        if any(w in label_lower for w in ("last name", "surname", "family name")):
            return p.last_name
        if "email" in label_lower:
            return p.email
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
