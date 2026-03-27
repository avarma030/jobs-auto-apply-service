"""LinkedIn applier — Easy Apply automation via Playwright.

Flow
----
1. ``setup()``  — launch Playwright browser, log in to LinkedIn.
2. ``apply()``  — navigate to the job page, click "Easy Apply", step through
                  the modal, and submit.

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
import re
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Page, TimeoutError as PWTimeoutError

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import Job, UserProfile
from src.utils.browser import BrowserManager

# ── Selectors ─────────────────────────────────────────────────────────────────

_LOGIN_URL = "https://www.linkedin.com/login"
_FEED_URL = "https://www.linkedin.com/feed/"

# Job page
_EASY_APPLY_BTN = (
    "button.jobs-apply-button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button--top-card[aria-label*='Easy Apply']"
)
_APPLY_BTN = "button.jobs-apply-button"  # generic apply (may be external)

# Modal container
_MODAL = "div.jobs-easy-apply-modal"

# Within the modal
_NEXT_BTN = (
    "button[aria-label='Continue to next step'], "
    "button[aria-label='Review your application'], "
    "footer button[aria-label*='Next']"
)
_SUBMIT_BTN = "button[aria-label='Submit application']"
_DISMISS_BTN = "button[aria-label='Dismiss']"
_ERROR_MSG = "div.artdeco-inline-feedback--error, p.artdeco-inline-feedback__message"

# Form field selectors (inside modal)
_TEXT_INPUTS = "input[type='text'], input[type='email'], input[type='tel'], input[type='number']"
_TEXTAREAS = "textarea"
_SELECTS = "select"
_RADIO_GROUPS = "fieldset"
_CHECKBOXES = "input[type='checkbox']"

# Resume upload
_FILE_INPUT = "input[type='file']"
_RESUME_CARD = "div.jobs-document-upload__card, label.jobs-document-upload-redesign-card"


class LinkedInApplier(BaseApplier):
    """Applies to LinkedIn Easy Apply jobs using Playwright browser automation."""

    board_name = "LinkedIn"
    board_slug = "linkedin"

    def __init__(self, profile: UserProfile):
        super().__init__(profile)
        self._bm: BrowserManager | None = None
        self._page: Page | None = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        await super().setup()
        self._bm = BrowserManager(
            user_data_dir=Path("data/.linkedin_session"),  # persist cookies
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

    async def apply(self, job: Job) -> ApplicationResult:
        if not self.can_apply(job):
            return self._skip(job, "Not a LinkedIn job")
        if not self._page:
            return self._fail(job, "Browser not initialised")

        try:
            if not job.easy_apply:
                return self._skip(job, "Job does not have Easy Apply — route to ATS applier")
            return await self._easy_apply(job)
        except Exception as exc:
            await self._bm.screenshot(self._page, f"error_{job.external_id}")
            return self._fail(job, str(exc))

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _ensure_logged_in(self) -> None:
        page = self._page
        creds = self.profile.job_board_accounts.linkedin
        if not creds or not creds.username or not creds.password:
            logger.warning("[LinkedIn] No credentials — some jobs may not be accessible")
            return

        # Check if already logged in (persistent context)
        await page.goto(_FEED_URL, wait_until="domcontentloaded")
        if await page.locator("div.feed-identity-module").count() > 0:
            logger.info("[LinkedIn] Already logged in (session cookie)")
            self._logged_in = True
            return

        logger.info("[LinkedIn] Logging in…")
        await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        await page.fill("input#username", creds.username)
        await page.fill("input#password", creds.password)
        await page.click("button[type='submit']")

        try:
            await page.wait_for_url("**/feed/**", timeout=15_000)
            self._logged_in = True
            logger.info("[LinkedIn] Login successful")
        except PWTimeoutError:
            # May land on a checkpoint / CAPTCHA page
            current = page.url
            if "checkpoint" in current or "challenge" in current:
                logger.warning(
                    "[LinkedIn] Login hit a checkpoint/CAPTCHA. "
                    "Run with headless=False and solve it manually, "
                    "then the session cookie will be saved for future runs."
                )
            else:
                logger.warning(f"[LinkedIn] Login may have failed — landed on: {current}")

    # ------------------------------------------------------------------
    # Easy Apply
    # ------------------------------------------------------------------

    async def _easy_apply(self, job: Job) -> ApplicationResult:
        page = self._page
        logger.info(f"[LinkedIn] Easy Applying to: {job.title} @ {job.company}")

        await page.goto(job.url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)  # let dynamic content settle

        # Click Easy Apply button
        btn = page.locator(_EASY_APPLY_BTN).first
        if await btn.count() == 0:
            # Maybe the page only has an external apply button
            generic_btn = page.locator(_APPLY_BTN).first
            if await generic_btn.count() > 0:
                label = await generic_btn.get_attribute("aria-label") or ""
                if "easy apply" not in label.lower():
                    return self._skip(job, "No Easy Apply button found — external apply only")
            return self._fail(job, "No apply button found on page")

        await btn.click()
        await page.wait_for_selector(_MODAL, timeout=10_000)
        logger.debug("[LinkedIn] Easy Apply modal opened")

        # Step through modal pages
        max_steps = 15
        for step in range(max_steps):
            await asyncio.sleep(0.8)

            # Check for submit button (final step)
            submit = page.locator(_SUBMIT_BTN).first
            if await submit.count() > 0 and await submit.is_visible():
                await submit.click()
                await asyncio.sleep(2)
                logger.info(f"[LinkedIn] Application submitted: {job.title} @ {job.company}")
                return self._ok(job)

            # Fill the current modal page
            await self._fill_modal_page(page, job)

            # Check for errors before proceeding
            error_el = page.locator(_ERROR_MSG).first
            if await error_el.count() > 0:
                error_text = await error_el.inner_text()
                logger.warning(f"[LinkedIn] Form error on step {step + 1}: {error_text}")
                await self._bm.screenshot(page, f"form_error_{job.external_id}_{step}")
                return self._fail(job, f"Form validation error: {error_text}")

            # Click Next / Continue / Review
            next_btn = page.locator(_NEXT_BTN).first
            if await next_btn.count() > 0 and await next_btn.is_enabled():
                await next_btn.click()
            else:
                # No next or submit button — stuck
                logger.warning(f"[LinkedIn] Stuck at step {step + 1}, no next/submit button")
                await self._bm.screenshot(page, f"stuck_{job.external_id}_{step}")
                return self._fail(job, f"Stuck at modal step {step + 1}")

        return self._fail(job, "Exceeded max modal steps without submitting")

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
                await inp.fill(str(value))
                logger.debug(f"[LinkedIn] Filled '{label}' = '{value}'")

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

            # Cover letter
            if "cover letter" in label_lower or "cover_letter" in label_lower:
                cl = self._build_cover_letter(job)
                await area.fill(cl)
                continue

            # Additional info / summary
            if "additional" in label_lower or "summary" in label_lower or "about" in label_lower:
                if self.profile.summary:
                    await area.fill(self.profile.summary)
                    continue

            value = self._answer_for_label(label)
            if value:
                await area.fill(str(value))

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
                # Try selecting by value first, then by label text
                try:
                    await sel.select_option(value=str(value))
                except Exception:
                    try:
                        await sel.select_option(label=str(value))
                    except Exception:
                        logger.debug(f"[LinkedIn] Could not select '{value}' for '{label}'")

    async def _fill_radio_buttons(self, page: Page) -> None:
        fieldsets = await page.locator(f"{_MODAL} {_RADIO_GROUPS}").all()
        for fieldset in fieldsets:
            if not await fieldset.is_visible():
                continue

            # Get question text from legend
            legend = await fieldset.locator("legend").first.inner_text() if await fieldset.locator("legend").count() > 0 else ""
            legend = legend.strip()

            answer = self._answer_for_label(legend)
            if answer is None:
                answer = self._infer_value_from_label(legend)
            if answer is None:
                continue

            # Find radio buttons
            radios = await fieldset.locator("input[type='radio']").all()
            for radio in radios:
                radio_label_el = page.locator(f"label[for='{await radio.get_attribute('id')}']")
                radio_label = ""
                if await radio_label_el.count() > 0:
                    radio_label = (await radio_label_el.inner_text()).strip()
                if radio_label.lower() == str(answer).lower() or await radio.get_attribute("value") == str(answer):
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
                continue

            should_check = str(answer).lower() in ("yes", "true", "1", "checked")
            if should_check and not await cb.is_checked():
                await cb.check()
            elif not should_check and await cb.is_checked():
                await cb.uncheck()

    async def _handle_resume_upload(self, page: Page) -> None:
        """Upload resume if a file input is present and no resume is already selected."""
        if not self.profile.resume_path or not Path(self.profile.resume_path).exists():
            return

        file_input = page.locator(f"{_MODAL} {_FILE_INPUT}").first
        if await file_input.count() == 0:
            return

        # Check if a resume card is already selected (LinkedIn may show previously uploaded)
        existing = page.locator(_RESUME_CARD).first
        if await existing.count() > 0:
            # A resume is already present — use it
            logger.debug("[LinkedIn] Resume already on file, skipping upload")
            return

        await file_input.set_input_files(str(self.profile.resume_path))
        logger.info(f"[LinkedIn] Uploaded resume: {self.profile.resume_path}")
        await asyncio.sleep(1.5)  # wait for upload to process

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
        """Build a basic cover letter from the template or a default."""
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
