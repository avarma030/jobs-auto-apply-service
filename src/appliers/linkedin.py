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
from playwright.async_api import Page, TimeoutError as PWTimeoutError

from src.appliers.base import ApplicationResult, BaseApplier
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
_EMAIL_SEL = "input[name='session_key'], #username"
_PASS_SEL  = "input[name='session_password'], #password"

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

# Job page — multiple selector variants to survive LinkedIn UI changes
_EASY_APPLY_BTN = (
    "button.jobs-apply-button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button--top-card[aria-label*='Easy Apply'], "
    "button[data-control-name='jobdetails_topcard_inapply']"
)
_APPLY_BTN = "button.jobs-apply-button"

# Already-applied state indicators
_ALREADY_APPLIED = (
    "span.artdeco-inline-feedback--success, "
    "div.post-apply-timeline__entity, "
    "button[aria-label*='Applied']"
)

# ── Modal selectors ────────────────────────────────────────────────────────────

_MODAL = "div.jobs-easy-apply-modal"

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
        self._unknown_questions = []
        return result

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
            if age > _COOKIE_MAX_AGE:
                logger.debug(f"[LinkedIn] Scraper cookies are {age / 3600:.1f}h old — too stale to inject")
                return False
            cookies = data.get("cookies", {})
            if not cookies.get("li_at"):
                logger.debug("[LinkedIn] Scraper cookies present but no li_at — skipping injection")
                return False
            # Convert flat dict → Playwright cookie objects
            pw_cookies = [
                {
                    "name": k,
                    "value": v,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "httpOnly": k in ("li_at", "JSESSIONID"),
                    "secure": True,
                    "sameSite": "None",
                }
                for k, v in cookies.items()
            ]
            await self._bm._context.add_cookies(pw_cookies)
            logger.info(
                f"[LinkedIn] Injected {len(pw_cookies)} warm scraper cookies "
                f"({age / 60:.0f} min old, li_at present)"
            )
            return True
        except Exception as exc:
            logger.warning(f"[LinkedIn] Cookie injection failed: {exc}")
            return False

    async def _is_logged_in(self, page: Page) -> bool:
        """Return True if the current page shows an active authenticated session."""
        url = page.url
        # URL-based check is fastest
        if any(p in url for p in ("/feed", "/jobs", "/mynetwork", "/messaging", "/in/")):
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
        if not username and settings.linkedin_email:
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
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error(f"[LinkedIn] Could not load login page: {exc}")
            return
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # Only fill the form if we actually landed on the login page
        if "/login" not in page.url and "/checkpoint" not in page.url:
            # Redirect means we may already be authenticated via persistent profile
            if await self._is_logged_in(page):
                logger.info("[LinkedIn] Persistent profile session detected — already logged in ✓")
                self._logged_in = True
                return

        # Fill email
        email_field = page.locator(_EMAIL_SEL).first
        try:
            await email_field.wait_for(state="visible", timeout=15_000)
        except PWTimeoutError:
            await self._bm.screenshot(page, "linkedin_login_no_email_field")
            logger.error(
                "[LinkedIn] Email input not found on login page. "
                "LinkedIn may be showing a CAPTCHA or challenge. "
                "Screenshot saved to data/screenshots/."
            )
            return

        await email_field.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await _BM.human_type(email_field, username)
        await asyncio.sleep(random.uniform(0.4, 0.9))

        # Fill password
        pass_field = page.locator(_PASS_SEL).first
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
        logger.info(f"[LinkedIn] Easy Apply → {job.title} @ {job.company}")

        # Navigate to job page
        try:
            await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            return self._fail(job, f"Could not navigate to job page: {exc}")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Detect mid-run session expiry
        if "/login" in page.url or "/authwall" in page.url:
            logger.info("[LinkedIn] Redirected to login mid-run — re-authenticating …")
            await self._ensure_logged_in()
            await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)

        # Already applied to this job?
        if await page.locator(_ALREADY_APPLIED).count() > 0:
            return self._skip(job, "Already applied to this job")

        # Dismiss overlay prompts before looking for the apply button
        await self._dismiss_prompts(page)

        # Find and click the Easy Apply button
        btn = page.locator(_EASY_APPLY_BTN).first
        if await btn.count() == 0:
            # Fallback: check if there's a generic apply button
            generic_btn = page.locator(_APPLY_BTN).first
            if await generic_btn.count() > 0:
                label = (await generic_btn.get_attribute("aria-label") or "").lower()
                if "easy apply" not in label:
                    return self._skip(job, "No Easy Apply button — external apply only")
            return self._fail(job, "No apply button found on page")

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
        logger.debug("[LinkedIn] Easy Apply modal opened")

        # Step through the multi-page modal
        max_steps = 15
        prev_step_label = ""
        stuck_count = 0

        for step in range(max_steps):
            await asyncio.sleep(random.uniform(0.6, 1.0))

            step_label = await self._get_step_label(page)
            logger.info(f"[LinkedIn] Modal step {step + 1}{': ' + step_label if step_label else ''}")

            # Final step: submit button is now visible
            submit = page.locator(_SUBMIT_BTN).first
            if await submit.count() > 0 and await submit.is_visible():
                await self._bm.screenshot(page, f"pre_submit_{job.external_id}")
                await submit.click()
                await asyncio.sleep(2)
                await self._bm.screenshot(page, f"post_submit_{job.external_id}")
                logger.info(f"[LinkedIn] ✓ Application submitted: {job.title} @ {job.company}")
                return self._ok(job)

            # Fill all visible fields on this modal page
            await self._fill_modal_page(page, job)

            # Check for validation errors before advancing
            error_el = page.locator(_ERROR_MSG).first
            if await error_el.count() > 0:
                error_text = await error_el.inner_text()
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
            next_btn = page.locator(_NEXT_BTN).first
            if await next_btn.count() > 0 and await next_btn.is_enabled():
                await next_btn.click()
                # Give the modal animation time to transition before the next fill pass
                await asyncio.sleep(0.5)
            else:
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
        inputs = await page.locator(f"{_MODAL} {_TEXT_INPUTS}").all()
        for inp in inputs:
            if not await inp.is_visible() or not await inp.is_enabled():
                continue
            current_val = await inp.input_value()
            if current_val.strip():
                continue  # already filled

            label = await self._get_field_label(page, inp)
            value = self._answer_for_label(label)
            if value is None:
                value = self._infer_value_from_label(label)

            if value is not None:
                await inp.fill("")
                await _BM.human_type(inp, str(value))
                logger.debug(f"[LinkedIn] Filled '{label}' = '{value}'")
            elif label:
                self._unknown_questions.append(label)
                logger.debug(f"[LinkedIn] No answer for text input: '{label}'")

    async def _fill_textareas(self, page: Page, job: Job) -> None:
        areas = await page.locator(f"{_MODAL} {_TEXTAREAS}").all()
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
                continue

            if "additional" in label_lower or "summary" in label_lower or "about" in label_lower:
                if self.profile.summary:
                    await area.fill(self.profile.summary)
                    continue

            value = self._answer_for_label(label)
            if value:
                await area.fill(str(value))
            elif label:
                self._unknown_questions.append(label)
                logger.debug(f"[LinkedIn] No answer for textarea: '{label}'")

    async def _fill_selects(self, page: Page) -> None:
        selects = await page.locator(f"{_MODAL} {_SELECTS}").all()
        for sel in selects:
            if not await sel.is_visible() or not await sel.is_enabled():
                continue
            current_val = await sel.input_value()
            if current_val and current_val not in ("", "Select an option"):
                continue

            label = await self._get_field_label(page, sel)
            value = self._answer_for_label(label)
            if value is None:
                value = self._infer_value_from_label(label)

            if value is not None:
                try:
                    await sel.select_option(value=str(value))
                except Exception:
                    try:
                        await sel.select_option(label=str(value))
                    except Exception:
                        logger.debug(f"[LinkedIn] Could not select '{value}' for '{label}'")
            elif label:
                self._unknown_questions.append(label)
                logger.debug(f"[LinkedIn] No answer for select: '{label}'")

    async def _fill_radio_buttons(self, page: Page) -> None:
        fieldsets = await page.locator(f"{_MODAL} {_RADIO_GROUPS}").all()
        for fieldset in fieldsets:
            if not await fieldset.is_visible():
                continue

            legend = ""
            if await fieldset.locator("legend").count() > 0:
                legend = (await fieldset.locator("legend").first.inner_text()).strip()

            answer = self._answer_for_label(legend)
            if answer is None:
                answer = self._infer_value_from_label(legend)
            if answer is None:
                if legend:
                    self._unknown_questions.append(legend)
                    logger.debug(f"[LinkedIn] No answer for radio: '{legend}'")
                continue

            radios = await fieldset.locator("input[type='radio']").all()
            for radio in radios:
                radio_id = await radio.get_attribute("id") or ""
                radio_label_el = page.locator(f"label[for='{radio_id}']")
                radio_label = ""
                if await radio_label_el.count() > 0:
                    radio_label = (await radio_label_el.inner_text()).strip()
                if (
                    radio_label.lower() == str(answer).lower()
                    or await radio.get_attribute("value") == str(answer)
                ):
                    await radio.check()
                    logger.debug(f"[LinkedIn] Radio '{legend}' = '{radio_label}'")
                    break

    async def _fill_checkboxes(self, page: Page) -> None:
        checkboxes = await page.locator(f"{_MODAL} {_CHECKBOXES}").all()
        for cb in checkboxes:
            if not await cb.is_visible():
                continue
            cb_id = await cb.get_attribute("id") or ""
            label_el = page.locator(f"label[for='{cb_id}']")
            label = (await label_el.inner_text()).strip() if await label_el.count() > 0 else ""

            answer = self._answer_for_label(label)
            if answer is None:
                if label:
                    self._unknown_questions.append(label)
                    logger.debug(f"[LinkedIn] No answer for checkbox: '{label}'")
                continue

            should_check = str(answer).lower() in ("yes", "true", "1", "checked")
            if should_check and not await cb.is_checked():
                await cb.check()
            elif not should_check and await cb.is_checked():
                await cb.uncheck()

    async def _handle_resume_upload(self, page: Page) -> None:
        """Upload resume — prefer tailored PDF, fall back to profile resume."""
        tailored = getattr(self, "_tailored_resume_path", None)
        if tailored and Path(tailored).exists():
            resume_path = Path(tailored)
            logger.info(f"[LinkedIn] Using tailored resume: {resume_path.name}")
        else:
            resume_path = Path(self.profile.resume_path) if self.profile.resume_path else None

        file_input = page.locator(f"{_MODAL} {_FILE_INPUT}").first
        if await file_input.count() == 0:
            return

        if not resume_path or not resume_path.exists():
            logger.debug("[LinkedIn] No resume file found, skipping upload")
            return

        await file_input.set_input_files(str(resume_path))
        logger.info(f"[LinkedIn] Uploaded resume: {resume_path.name}")
        try:
            await page.wait_for_selector(f"{_MODAL} {_RESUME_CARD}", timeout=8_000)
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
