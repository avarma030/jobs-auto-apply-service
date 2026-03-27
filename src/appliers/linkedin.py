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
from src.utils.browser import BrowserManager, BrowserManager as _BM

# ── Selectors ─────────────────────────────────────────────────────────────────

_LOGIN_URL = "https://www.linkedin.com/login"
_FEED_URL = "https://www.linkedin.com/feed/"

# Job page — multiple selector variants to survive LinkedIn UI changes
_EASY_APPLY_BTN = (
    "button.jobs-apply-button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button--top-card[aria-label*='Easy Apply'], "
    "button[data-control-name='jobdetails_topcard_inapply']"
)
_APPLY_BTN = "button.jobs-apply-button"

# "Follow" prompt that sometimes blocks the Easy Apply button
_FOLLOW_PROMPT_DISMISS = (
    "button[aria-label='Dismiss'], "
    "button[aria-label='Got it'], "
    "button.artdeco-modal__dismiss"
)

# Modal container
_MODAL = "div.jobs-easy-apply-modal"

# Progress indicator — "Step X of Y"
_STEP_INDICATOR = (
    "span.jobs-easy-apply-form-section__grouping-title, "
    "div.ph5 span.t-14"
)

# Within the modal
_NEXT_BTN = (
    "button[aria-label='Continue to next step'], "
    "button[aria-label='Review your application'], "
    "footer button[aria-label*='Next'], "
    "button.artdeco-button--primary[type='button']"
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

        try:
            if not job.easy_apply:
                return self._skip(job, "Job does not have Easy Apply — route to ATS applier")
            return await self._easy_apply(job, tailored_resume_path=tailored_resume_path, cover_letter=cover_letter)
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

    async def _easy_apply(
        self,
        job: Job,
        tailored_resume_path: str | None = None,
        cover_letter: str | None = None,
    ) -> ApplicationResult:
        page = self._page
        self._tailored_resume_path = tailored_resume_path
        self._cover_letter_text = cover_letter
        logger.info(f"[LinkedIn] Easy Applying to: {job.title} @ {job.company}")

        await page.goto(job.url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        # Dismiss any follow/notification prompts that block the apply button
        await self._dismiss_prompts(page)

        # Click Easy Apply button
        btn = page.locator(_EASY_APPLY_BTN).first
        if await btn.count() == 0:
            generic_btn = page.locator(_APPLY_BTN).first
            if await generic_btn.count() > 0:
                label = await generic_btn.get_attribute("aria-label") or ""
                if "easy apply" not in label.lower():
                    return self._skip(job, "No Easy Apply button found — external apply only")
            return self._fail(job, "No apply button found on page")

        await btn.click()
        try:
            await page.wait_for_selector(_MODAL, timeout=10_000)
        except PWTimeoutError:
            await self._bm.screenshot(page, f"modal_timeout_{job.external_id}")
            return self._fail(job, "Easy Apply modal did not open (timeout)")
        logger.debug("[LinkedIn] Easy Apply modal opened")

        max_steps = 15
        for step in range(max_steps):
            await asyncio.sleep(0.8)

            # Log step progress if LinkedIn shows it
            step_label = await self._get_step_label(page)
            logger.info(f"[LinkedIn] Modal step {step + 1}{': ' + step_label if step_label else ''}")

            # Final step: submit button visible
            submit = page.locator(_SUBMIT_BTN).first
            if await submit.count() > 0 and await submit.is_visible():
                await self._bm.screenshot(page, f"pre_submit_{job.external_id}")
                await submit.click()
                await asyncio.sleep(2)
                await self._bm.screenshot(page, f"post_submit_{job.external_id}")
                logger.info(f"[LinkedIn] ✓ Application submitted: {job.title} @ {job.company}")
                return self._ok(job)

            # Fill visible fields on this page
            await self._fill_modal_page(page, job)

            # Check for validation errors before clicking Next
            error_el = page.locator(_ERROR_MSG).first
            if await error_el.count() > 0:
                error_text = await error_el.inner_text()
                logger.warning(f"[LinkedIn] Validation error at step {step + 1}: {error_text}")
                await self._bm.screenshot(page, f"form_error_{job.external_id}_{step}")
                return self._fail(job, f"Form validation error: {error_text}")

            # Advance to next step
            next_btn = page.locator(_NEXT_BTN).first
            if await next_btn.count() > 0 and await next_btn.is_enabled():
                await next_btn.click()
            else:
                logger.warning(f"[LinkedIn] No Next/Submit at step {step + 1} — stuck")
                await self._bm.screenshot(page, f"stuck_{job.external_id}_{step}")
                return self._fail(job, f"Stuck at modal step {step + 1}")

        return self._fail(job, "Exceeded max modal steps without submitting")

    async def _dismiss_prompts(self, page: Page) -> None:
        """Dismiss any overlay prompts (Follow, notifications) that cover the apply button."""
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
                await inp.fill("")  # clear first
                await _BM.human_type(inp, str(value))
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

            # Cover letter — prefer AI-generated text injected via apply()
            if "cover letter" in label_lower or "cover_letter" in label_lower:
                cl_text = getattr(self, "_cover_letter_text", None)
                cl = cl_text if cl_text else self._build_cover_letter(job)
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
        """Upload resume — prefer tailored PDF, fall back to profile resume."""
        # Tailored resume takes priority; otherwise fall back to profile resume
        tailored = getattr(self, "_tailored_resume_path", None)
        if tailored and Path(tailored).exists():
            resume_path = Path(tailored)
            logger.info(f"[LinkedIn] Using tailored resume: {resume_path.name}")
        else:
            resume_path = Path(self.profile.resume_path) if self.profile.resume_path else None

        # If LinkedIn shows an already-uploaded resume card, click the upload button
        # to replace it with our tailored version (only if we have one).
        file_input = page.locator(f"{_MODAL} {_FILE_INPUT}").first
        if await file_input.count() == 0:
            return

        if not resume_path or not resume_path.exists():
            logger.debug("[LinkedIn] No resume file found, skipping upload")
            return

        await file_input.set_input_files(str(resume_path))
        logger.info(f"[LinkedIn] Uploaded resume: {resume_path.name}")
        # Wait for LinkedIn to process the upload (shows a progress indicator)
        try:
            await page.wait_for_selector(
                f"{_MODAL} {_RESUME_CARD}",
                timeout=8_000,
            )
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
