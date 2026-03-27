"""LinkedIn Easy Apply automation backed by a saved Playwright session."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from src.appliers.base import ApplicationResult, BaseApplier
from src.models import ApplicationPackage, Job
from src.utils.browser import BrowserManager, BrowserManager as _BM

_FEED_URL = "https://www.linkedin.com/feed/"
_SESSION_DIR = Path("data/.linkedin_session")
_MODAL = "div.jobs-easy-apply-modal"
_EASY_APPLY_BTN = (
    "button.jobs-apply-button[aria-label*='Easy Apply'], "
    "button.jobs-apply-button--top-card[aria-label*='Easy Apply'], "
    "button[data-control-name='jobdetails_topcard_inapply']"
)
_APPLY_BTN = "button.jobs-apply-button"
_DISMISS_PROMPTS = (
    "button[aria-label='Dismiss'], "
    "button[aria-label='Got it'], "
    "button.artdeco-modal__dismiss"
)
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
_SUBMIT_BTN = "button[aria-label='Submit application']"
_FILE_INPUT = "input[type='file']"
_RESUME_CARD = (
    "div.jobs-document-upload__card, "
    "label.jobs-document-upload-redesign-card, "
    "li.jobs-resume-picker__resume"
)
_TEXT_INPUTS = (
    "input[type='text'], input[type='email'], input[type='tel'], input[type='number']"
)
_TEXTAREAS = "textarea"
_SELECTS = "select"
_RADIOS = "fieldset"
_CHECKBOXES = "input[type='checkbox']"
_ERROR_MSG = "div.artdeco-inline-feedback--error, p.artdeco-inline-feedback__message"


class LinkedInApplier(BaseApplier):
    """Apply to LinkedIn Easy Apply jobs using a saved persistent session."""

    board_name = "LinkedIn"
    board_slug = "linkedin"

    def __init__(self, profile):
        super().__init__(profile)
        self._bm: BrowserManager | None = None
        self._page: Any | None = None
        self._session_error: str | None = None

    async def setup(self) -> None:
        await super().setup()
        self._bm = BrowserManager(user_data_dir=_SESSION_DIR)
        await self._bm.start()
        self._page = await self._bm.new_page()
        await self._ensure_logged_in()

    async def teardown(self) -> None:
        if self._bm is not None:
            await self._bm.stop()
        self._bm = None
        self._page = None
        await super().teardown()

    async def apply(self, job: Job, package: ApplicationPackage | None = None) -> ApplicationResult:
        if not self.can_apply(job):
            return self._skip(job, "Not a LinkedIn job.")
        if not job.easy_apply:
            return self._skip(job, "LinkedIn external-apply jobs are out of scope for phase one.")
        if self._session_error:
            return self._skip(job, self._session_error)
        if self._page is None:
            return self._fail(job, "Browser is not initialised.")

        try:
            return await self._easy_apply(job, package=package)
        except Exception as exc:
            if self._bm is not None and self._page is not None:
                await self._bm.screenshot(self._page, f"linkedin_error_{job.external_id or 'job'}")
            return self._fail(job, str(exc))

    async def _ensure_logged_in(self) -> None:
        assert self._page is not None
        await self._page.goto(_FEED_URL, wait_until="domcontentloaded")
        await _BM.human_pause(0.4, 0.8)

        current_url = str(getattr(self._page, "url", "")).lower()
        username_field = self._page.locator("input#username")
        if "login" in current_url or "checkpoint" in current_url or await username_field.count() > 0:
            self._session_error = (
                "LinkedIn session not found or expired. Run `python main.py login linkedin` "
                "to refresh the saved session."
            )
            logger.warning(f"[LinkedIn] {self._session_error}")
            return

        self._session_error = None
        logger.info("[LinkedIn] Saved session loaded successfully")

    async def _easy_apply(
        self,
        job: Job,
        *,
        package: ApplicationPackage | None = None,
    ) -> ApplicationResult:
        assert self._page is not None
        page = self._page
        logger.info(f"[LinkedIn] Easy applying to {job.title} @ {job.company}")

        await page.goto(job.url, wait_until="domcontentloaded")
        await _BM.human_pause(0.8, 1.3)
        await self._dismiss_prompts(page)

        easy_apply = page.locator(_EASY_APPLY_BTN).first
        if await easy_apply.count() == 0:
            generic_apply = page.locator(_APPLY_BTN).first
            if await generic_apply.count() > 0:
                label = (await generic_apply.get_attribute("aria-label") or "").lower()
                if "easy apply" not in label:
                    return self._skip(job, "LinkedIn page no longer exposes Easy Apply for this role.")
            return self._fail(job, "Easy Apply button was not found on the job page.")

        await _BM.human_click(easy_apply, page)
        try:
            await page.wait_for_selector(_MODAL, timeout=10_000)
        except Exception:
            if self._bm is not None:
                await self._bm.screenshot(page, f"linkedin_modal_timeout_{job.external_id or 'job'}")
            return self._fail(job, "Easy Apply modal did not open.")

        for step in range(12):
            await _BM.human_pause(0.5, 0.9)
            step_label = await self._get_step_label(page)
            logger.info(
                f"[LinkedIn] Modal step {step + 1}{': ' + step_label if step_label else ''}"
            )

            issue = await self._fill_modal_page(page, job, package=package)
            if issue:
                if self._bm is not None:
                    await self._bm.screenshot(page, f"linkedin_review_{job.external_id or 'job'}_{step}")
                await self._dismiss_modal(page)
                return self._skip(job, issue)

            validation_error = await self._read_validation_error(page)
            if validation_error:
                if self._bm is not None:
                    await self._bm.screenshot(page, f"linkedin_validation_{job.external_id or 'job'}_{step}")
                await self._dismiss_modal(page)
                return self._skip(job, f"LinkedIn form needs review: {validation_error}")

            submit = page.locator(_SUBMIT_BTN).first
            if await submit.count() > 0 and await submit.is_visible():
                await _BM.human_click(submit, page)
                await _BM.human_pause(1.2, 1.8)
                return self._ok(job, confirmation_id=job.external_id)

            next_button = page.locator(_NEXT_BTN).first
            if await next_button.count() == 0 or not await next_button.is_enabled():
                if self._bm is not None:
                    await self._bm.screenshot(page, f"linkedin_stuck_{job.external_id or 'job'}_{step}")
                await self._dismiss_modal(page)
                return self._fail(job, "LinkedIn modal could not advance to the next step.")
            await _BM.human_click(next_button, page)

        await self._dismiss_modal(page)
        return self._fail(job, "LinkedIn modal exceeded the maximum number of steps.")

    async def _dismiss_prompts(self, page: Any) -> None:
        for selector in _DISMISS_PROMPTS.split(", "):
            try:
                element = page.locator(selector.strip()).first
                if await element.count() > 0 and await element.is_visible():
                    await element.click()
                    await _BM.human_pause(0.1, 0.2)
            except Exception:
                continue

    async def _dismiss_modal(self, page: Any) -> None:
        dismiss = page.locator("button[aria-label='Dismiss']").first
        if await dismiss.count() > 0:
            try:
                await dismiss.click()
            except Exception:
                pass

    async def _get_step_label(self, page: Any) -> str:
        try:
            label = page.locator(_STEP_INDICATOR).first
            if await label.count() > 0:
                return (await label.inner_text()).strip()
        except Exception:
            return ""
        return ""

    async def _fill_modal_page(
        self,
        page: Any,
        job: Job,
        *,
        package: ApplicationPackage | None,
    ) -> str | None:
        for method in (
            self._fill_text_inputs,
            self._fill_textareas,
            self._fill_selects,
            self._fill_radio_buttons,
            self._fill_checkboxes,
            self._handle_resume_upload,
        ):
            issue = await method(page, job, package)
            if issue:
                return issue
        return None

    async def _fill_text_inputs(
        self,
        page: Any,
        _job: Job,
        _package: ApplicationPackage | None,
    ) -> str | None:
        inputs = await page.locator(f"{_MODAL} {_TEXT_INPUTS}").all()
        for control in inputs:
            if not await self._is_actionable(control):
                continue
            current_value = (await control.input_value()).strip()
            if current_value:
                continue

            label = await self._get_field_label(page, control)
            answer = self._answer_for_label(label) or self._infer_value_from_label(label)
            if answer is None:
                if await self._is_required(control):
                    return self._manual_review_reason(label)
                continue

            await control.fill("")
            await _BM.human_type(control, str(answer))
        return None

    async def _fill_textareas(
        self,
        page: Any,
        job: Job,
        package: ApplicationPackage | None,
    ) -> str | None:
        areas = await page.locator(f"{_MODAL} {_TEXTAREAS}").all()
        for control in areas:
            if not await self._is_actionable(control):
                continue
            if (await control.input_value()).strip():
                continue

            label = await self._get_field_label(page, control)
            lowered = label.lower()
            answer: str | None = None

            if "cover letter" in lowered:
                answer = self._build_cover_letter(job, package)
            elif any(token in lowered for token in ("summary", "about", "additional")):
                answer = self.profile.summary or self.profile.headline or ""
            else:
                answer = self._answer_for_label(label)

            if answer is None:
                if await self._is_required(control):
                    return self._manual_review_reason(label)
                continue

            await control.fill(str(answer))
        return None

    async def _fill_selects(
        self,
        page: Any,
        _job: Job,
        _package: ApplicationPackage | None,
    ) -> str | None:
        selects = await page.locator(f"{_MODAL} {_SELECTS}").all()
        for control in selects:
            if not await self._is_actionable(control):
                continue

            current_value = (await control.input_value()).strip()
            if current_value and current_value.lower() not in {"", "select an option"}:
                continue

            label = await self._get_field_label(page, control)
            answer = self._answer_for_label(label) or self._infer_value_from_label(label)
            if answer is None:
                if await self._is_required(control):
                    return self._manual_review_reason(label)
                continue

            if not await self._select_option(control, str(answer)):
                return self._manual_review_reason(label)
        return None

    async def _fill_radio_buttons(
        self,
        page: Any,
        _job: Job,
        _package: ApplicationPackage | None,
    ) -> str | None:
        groups = await page.locator(f"{_MODAL} {_RADIOS}").all()
        for fieldset in groups:
            if not await fieldset.is_visible():
                continue

            legend = ""
            legend_locator = fieldset.locator("legend").first
            if await legend_locator.count() > 0:
                legend = (await legend_locator.inner_text()).strip()
            if not legend:
                continue

            checked = await fieldset.locator("input[type='radio']:checked").count()
            if checked > 0:
                continue

            answer = self._answer_for_label(legend) or self._infer_value_from_label(legend)
            if answer is None:
                return self._manual_review_reason(legend)

            options = await fieldset.locator("input[type='radio']").all()
            matched = False
            for option in options:
                option_id = await option.get_attribute("id")
                option_label = ""
                if option_id:
                    label_locator = page.locator(f"label[for='{option_id}']").first
                    if await label_locator.count() > 0:
                        option_label = (await label_locator.inner_text()).strip()
                option_value = await option.get_attribute("value") or ""
                if self._answers_match(str(answer), option_label, option_value):
                    await option.check()
                    matched = True
                    break

            if not matched:
                return self._manual_review_reason(legend)
        return None

    async def _fill_checkboxes(
        self,
        page: Any,
        _job: Job,
        _package: ApplicationPackage | None,
    ) -> str | None:
        controls = await page.locator(f"{_MODAL} {_CHECKBOXES}").all()
        for control in controls:
            if not await control.is_visible():
                continue

            label = await self._checkbox_label(page, control)
            answer = self._answer_for_label(label)
            if answer is None:
                if await self._is_required(control):
                    return self._manual_review_reason(label)
                continue

            should_check = str(answer).strip().lower() in {"yes", "true", "1", "checked"}
            if should_check and not await control.is_checked():
                await control.check()
            elif not should_check and await control.is_checked():
                await control.uncheck()
        return None

    async def _handle_resume_upload(
        self,
        page: Any,
        _job: Job,
        package: ApplicationPackage | None,
    ) -> str | None:
        resume_path: Path | None = None
        if package and package.resume_path:
            resume_path = Path(package.resume_path)
        elif self.profile.resume_path:
            resume_path = Path(self.profile.resume_path)

        file_input = page.locator(f"{_MODAL} {_FILE_INPUT}").first
        if await file_input.count() > 0:
            if resume_path is None or not resume_path.exists():
                return "Tailored resume file is missing for this LinkedIn application."
            await file_input.set_input_files(str(resume_path))
            await _BM.human_pause(0.6, 1.0)
            return None

        existing_card = page.locator(_RESUME_CARD).first
        if await existing_card.count() > 0:
            try:
                await existing_card.click()
            except Exception:
                pass
        return None

    async def _read_validation_error(self, page: Any) -> str | None:
        try:
            error = page.locator(_ERROR_MSG).first
            if await error.count() > 0 and await error.is_visible():
                return (await error.inner_text()).strip()
        except Exception:
            return None
        return None

    async def _get_field_label(self, page: Any, element: Any) -> str:
        element_id = await element.get_attribute("id") or ""
        element_name = await element.get_attribute("name") or ""
        placeholder = await element.get_attribute("placeholder") or ""

        if element_id:
            label = page.locator(f"label[for='{element_id}']").first
            if await label.count() > 0:
                return (await label.inner_text()).strip()

        try:
            parent_label = await element.evaluate(
                "(el) => {"
                "let current = el.parentElement;"
                "for (let i = 0; i < 5; i++) {"
                "  if (!current) break;"
                "  const label = current.querySelector('label');"
                "  if (label) return label.innerText.trim();"
                "  current = current.parentElement;"
                "}"
                "return '';"
                "}"
            )
            if parent_label:
                return str(parent_label).strip()
        except Exception:
            pass

        return placeholder.strip() or element_name.strip() or "LinkedIn question"

    async def _checkbox_label(self, page: Any, element: Any) -> str:
        element_id = await element.get_attribute("id") or ""
        if element_id:
            label = page.locator(f"label[for='{element_id}']").first
            if await label.count() > 0:
                return (await label.inner_text()).strip()
        return await self._get_field_label(page, element)

    def _answer_for_label(self, label: str) -> str | None:
        lowered = label.lower().strip()
        for question, answer in self.profile.custom_answers.items():
            question_lower = question.lower().strip()
            if question_lower in lowered or lowered in question_lower:
                return answer
        return None

    def _infer_value_from_label(self, label: str) -> str | None:
        lowered = label.lower()
        profile = self.profile

        if any(token in lowered for token in ("first name", "given name")):
            return profile.first_name
        if any(token in lowered for token in ("last name", "surname", "family name")):
            return profile.last_name
        if "email" in lowered:
            return profile.email
        if "phone" in lowered or "mobile" in lowered:
            return profile.phone or ""
        if "linkedin" in lowered:
            return profile.social_links.linkedin or ""
        if "github" in lowered:
            return profile.social_links.github or ""
        if "portfolio" in lowered or "website" in lowered:
            return profile.social_links.portfolio or ""
        if "city" in lowered:
            return profile.address.city if profile.address else ""
        if "state" in lowered or "province" in lowered:
            return profile.address.state if profile.address else ""
        if "zip" in lowered or "postal" in lowered:
            return profile.address.zip_code if profile.address else ""
        if "country" in lowered:
            return profile.address.country if profile.address else "US"
        if "years of experience" in lowered or "years experience" in lowered:
            return str(profile.years_of_experience) if profile.years_of_experience is not None else None
        if "summary" in lowered or "headline" in lowered:
            return profile.summary or profile.headline or ""
        return None

    def _build_cover_letter(self, job: Job, package: ApplicationPackage | None) -> str:
        if package and package.cover_letter_text:
            return package.cover_letter_text
        if self.profile.cover_letter_template_path:
            template_path = Path(self.profile.cover_letter_template_path)
            if template_path.exists():
                template = template_path.read_text(encoding="utf-8")
                return (
                    template.replace("{job_title}", job.title)
                    .replace("{company}", job.company)
                    .replace("{first_name}", self.profile.first_name)
                    .replace("{last_name}", self.profile.last_name)
                )
        summary = self.profile.summary or (
            f"I bring {self.profile.years_of_experience or 'several'} years of experience "
            "that align strongly with this opportunity."
        )
        return (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {job.title} role at {job.company}. "
            f"{summary}\n\n"
            f"Best regards,\n{self.profile.first_name} {self.profile.last_name}"
        )

    @staticmethod
    async def _is_actionable(element: Any) -> bool:
        try:
            return await element.is_visible() and await element.is_enabled()
        except Exception:
            return False

    @staticmethod
    async def _is_required(element: Any) -> bool:
        for attribute in ("required", "aria-required"):
            try:
                value = await element.get_attribute(attribute)
            except Exception:
                value = None
            if value and value.lower() != "false":
                return True
        return False

    @staticmethod
    async def _select_option(control: Any, answer: str) -> bool:
        try:
            await control.select_option(label=answer)
            return True
        except Exception:
            try:
                await control.select_option(value=answer)
                return True
            except Exception:
                return False

    @staticmethod
    def _answers_match(answer: str, option_label: str, option_value: str) -> bool:
        normalized = answer.strip().lower()
        return normalized in {
            option_label.strip().lower(),
            option_value.strip().lower(),
        }

    @staticmethod
    def _manual_review_reason(label: str) -> str:
        field = label.strip() or "an unknown LinkedIn screening field"
        return f"Manual review required for LinkedIn question: {field}"
