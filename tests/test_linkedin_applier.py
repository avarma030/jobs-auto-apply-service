"""Unit tests for the LinkedIn applier.

Browser interactions are fully mocked — no real Playwright needed.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.appliers.linkedin as linkedin_mod
from src.appliers.base import ApplicationResult
from src.appliers.linkedin import LinkedInApplier
from src.models import ApplicationStatus, Job, UserProfile
from src.models.user_profile import (
    Address,
    ApplicationPreferences,
    JobBoardAccounts,
    JobBoardCredentials,
    SocialLinks,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_profile(**overrides) -> UserProfile:
    base = dict(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+1-555-123-4567",
        address=Address(city="San Francisco", state="CA", zip_code="94105", country="US"),
        headline="Senior Software Engineer",
        summary="Experienced engineer.",
        years_of_experience=7,
        skills=["Python", "Django", "PostgreSQL"],
        social_links=SocialLinks(
            linkedin="https://linkedin.com/in/janedoe",
            github="https://github.com/janedoe",
        ),
        job_board_accounts=JobBoardAccounts(
            linkedin=JobBoardCredentials(
                username="jane@example.com", password="secret"
            )
        ),
        preferences=ApplicationPreferences(),
        custom_answers={
            "Are you legally authorized to work in the US?": "Yes",
            "Will you require sponsorship?": "No",
            "Years of experience with Python": "7",
        },
    )
    base.update(overrides)
    return UserProfile(**base)


def make_job(easy_apply: bool = True, **overrides) -> Job:
    base = dict(
        title="Senior Python Engineer",
        company="Acme Corp",
        url="https://www.linkedin.com/jobs/view/1234567890/",
        source_board="linkedin",
        external_id="1234567890",
        easy_apply=easy_apply,
    )
    base.update(overrides)
    return Job(**base)


def make_applier(profile: UserProfile | None = None) -> LinkedInApplier:
    applier = LinkedInApplier.__new__(LinkedInApplier)
    applier.profile = profile or make_profile()
    applier._bm = None
    applier._page = None
    applier._logged_in = False
    applier._unknown_question_prompts = {}
    applier._answered_questions = []
    applier._learned_answers = {}
    applier._answered_question_index = {}
    return applier


class _FakeCheckbox:
    def __init__(self, checked: bool = False, name: str = "jobDetailsEasyApplyTopChoiceCheckbox"):
        self.checked = checked
        self.name = name
        self.calls: list[tuple] = []

    async def is_checked(self) -> bool:
        return self.checked

    async def check(self, timeout=None, force=False) -> None:
        self.calls.append(("check", timeout, force))
        raise RuntimeError("pointer intercepted")

    async def uncheck(self, timeout=None, force=False) -> None:
        self.calls.append(("uncheck", timeout, force))
        raise RuntimeError("pointer intercepted")

    async def evaluate(self, _script, desired) -> None:
        self.calls.append(("evaluate", desired))
        self.checked = desired

    async def get_attribute(self, name: str) -> str | None:
        if name == "name":
            return self.name
        return None


class _FakeLabel:
    def __init__(self, target):
        self.target = target
        self.calls: list[tuple] = []
        self.first = self

    async def click(self, timeout=None, force=False) -> None:
        self.calls.append(("click", timeout, force))
        self.target.checked = True


class _FakeRadio:
    def __init__(self, checked: bool = False, value: str = "Yes", name: str = "visaSponsorship"):
        self.checked = checked
        self.value = value
        self.name = name
        self.calls: list[tuple] = []

    async def is_checked(self) -> bool:
        return self.checked

    async def check(self, timeout=None, force=False) -> None:
        self.calls.append(("check", timeout, force))
        raise RuntimeError("pointer intercepted")

    async def evaluate(self, _script) -> None:
        self.calls.append(("evaluate", None))
        self.checked = True

    async def get_attribute(self, name: str) -> str | None:
        if name == "name":
            return self.name
        if name == "value":
            return self.value
        return None


class _FakeButton:
    def __init__(self):
        self.calls: list[tuple] = []

    async def scroll_into_view_if_needed(self) -> None:
        self.calls.append(("scroll",))

    async def click(self, timeout=None, force=False) -> None:
        self.calls.append(("click", timeout, force))
        if not force:
            raise RuntimeError("overlay intercepted")

    async def dispatch_event(self, event_name: str, timeout=None) -> None:
        self.calls.append(("dispatch_event", event_name, timeout))


class _FakeKeyboard:
    def __init__(self):
        self.calls: list[str] = []

    async def press(self, key: str) -> None:
        self.calls.append(key)


class _FakeOverlayLocator:
    def __init__(self, visible: bool):
        self.visible = visible
        self.first = self
        self.clicks: list[tuple] = []

    async def count(self) -> int:
        return 1 if self.visible else 0

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self, timeout=None, force=False) -> None:
        self.clicks.append((timeout, force))
        self.visible = False


class _FakePageWithOverlay:
    def __init__(self, visible: bool = True):
        self.visible = visible
        self.keyboard = _FakeKeyboard()
        self.evaluations: list[str] = []
        self.overlay = _FakeOverlayLocator(visible)

    def locator(self, _selector: str):
        return self.overlay

    async def evaluate(self, script: str) -> None:
        self.evaluations.append(script)
        self.visible = False
        self.overlay.visible = False


class _FakeDismissButton:
    def __init__(self, skip_click: bool = False):
        self.skip_click = skip_click
        self.first = self
        self.clicks: list[tuple] = []

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, _script) -> bool:
        return self.skip_click

    async def click(self, timeout=None, force=False) -> None:
        self.clicks.append((timeout, force))


class _FakePromptPage:
    def __init__(self, skip_click: bool = False):
        self.dismiss = _FakeDismissButton(skip_click=skip_click)
        self.empty = _FakeOverlayLocator(False)

    def locator(self, selector: str):
        if selector == "button[aria-label='Dismiss']":
            return self.dismiss
        return self.empty


class _FakeSaveDialogCloseButton:
    def __init__(self, dialog):
        self.dialog = dialog
        self.first = self
        self.clicks: list[tuple] = []

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.dialog.visible

    async def click(self, timeout=None, force=False) -> None:
        self.clicks.append((timeout, force))
        self.dialog.visible = False


class _FakeSaveDialogLocator:
    def __init__(self, visible: bool = True):
        self.visible = visible
        self.first = self
        self.close_button = _FakeSaveDialogCloseButton(self)
        self.empty = _FakeOverlayLocator(False)

    def filter(self, **_kwargs):
        return self

    async def count(self) -> int:
        return 1 if self.visible else 0

    async def is_visible(self) -> bool:
        return self.visible

    def locator(self, selector: str):
        if selector in {
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            "button.artdeco-modal__dismiss",
        }:
            return self.close_button
        return self.empty


class _FakeSaveDialogPage:
    def __init__(self, visible: bool = True):
        self.dialog = _FakeSaveDialogLocator(visible=visible)
        self.empty = _FakeOverlayLocator(False)

    def locator(self, selector: str):
        if selector == "div[role='dialog']":
            return self.dialog
        return self.empty


class _FakeFileInput:
    def __init__(self, accept: str = "", name: str = "upload", visible: bool = True, enabled: bool = True):
        self.accept = accept
        self.name = name
        self.visible = visible
        self.enabled = enabled
        self.uploads: list[str] = []

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def get_attribute(self, name: str) -> str | None:
        mapping = {
            "accept": self.accept,
            "name": self.name,
        }
        return mapping.get(name)

    async def set_input_files(self, path: str) -> None:
        self.uploads.append(path)

    async def evaluate(self, _script: str) -> str:
        return ""


class _FakeAllLocator:
    def __init__(self, items):
        self.items = items

    async def all(self):
        return self.items


class _FakeResumePage:
    def __init__(self, inputs):
        self.inputs = inputs
        self.wait_calls: list[tuple[str, int | None]] = []

    def locator(self, _selector: str):
        return _FakeAllLocator(self.inputs)

    async def wait_for_selector(self, selector: str, timeout=None) -> None:
        self.wait_calls.append((selector, timeout))


# ── Tests: can_apply ──────────────────────────────────────────────────────────

class TestCanApply:
    def test_linkedin_job(self):
        applier = make_applier()
        job = make_job(source_board="linkedin")
        assert applier.can_apply(job) is True

    def test_non_linkedin_job(self):
        applier = make_applier()
        job = make_job(source_board="indeed")
        assert applier.can_apply(job) is False


# ── Tests: apply routing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_skips_non_linkedin_job():
    applier = make_applier()
    job = make_job(source_board="indeed")
    result = await applier.apply(job)
    assert result.status == ApplicationStatus.SKIPPED


@pytest.mark.asyncio
async def test_apply_ignores_stale_easy_apply_flag_and_checks_live_page():
    applier = make_applier()
    applier._page = MagicMock()
    applier._page.url = "https://www.linkedin.com/jobs/"
    applier._logged_in = True
    applier._is_logged_in = AsyncMock(return_value=True)
    job = make_job(easy_apply=False)
    expected = ApplicationResult(job, ApplicationStatus.SKIPPED, "checked live page")
    applier._easy_apply = AsyncMock(return_value=expected)
    result = await applier.apply(job)
    assert result is expected
    applier._easy_apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_fails_if_browser_not_initialised():
    applier = make_applier()
    applier._page = None
    job = make_job()
    result = await applier.apply(job)
    assert result.status == ApplicationStatus.FAILED
    assert "Browser not initialised" in result.message


@pytest.mark.asyncio
async def test_inject_scraper_cookies_keeps_jsessionid_js_readable(tmp_path):
    cookie_path = tmp_path / "linkedin_cookies.json"
    cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "jane@example.com",
                "cookies": {
                    "li_at": "li-at-token",
                    "JSESSIONID": '"ajax:123"',
                    "lang": "v=2&lang=en-us",
                },
            }
        ),
        encoding="utf-8",
    )

    applier = make_applier()
    applier._bm = MagicMock()
    applier._bm._context = MagicMock()
    applier._bm._context.add_cookies = AsyncMock()
    applier._ensure_linkedin_state = MagicMock()
    applier._cookie_path = cookie_path
    applier._linkedin_identity = "jane@example.com"

    injected = await applier._inject_scraper_cookies()

    assert injected is True
    applier._bm._context.add_cookies.assert_awaited_once()
    cookies = applier._bm._context.add_cookies.await_args.args[0]
    by_name = {cookie["name"]: cookie for cookie in cookies}
    assert by_name["li_at"]["httpOnly"] is True
    assert by_name["JSESSIONID"]["httpOnly"] is False


@pytest.mark.asyncio
async def test_inject_scraper_cookies_falls_back_to_legacy_cookie_file(tmp_path):
    scoped_cookie_path = tmp_path / "scoped_cookies.json"
    scoped_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {"lang": "en-us"},
            }
        ),
        encoding="utf-8",
    )
    legacy_cookie_path = tmp_path / "legacy_cookies.json"
    legacy_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {
                    "li_at": "legacy-li-at",
                    "JSESSIONID": '"ajax:456"',
                },
            }
        ),
        encoding="utf-8",
    )

    applier = make_applier()
    applier._bm = MagicMock()
    applier._bm._context = MagicMock()
    applier._bm._context.add_cookies = AsyncMock()
    applier._ensure_linkedin_state = MagicMock()
    applier._cookie_path = scoped_cookie_path
    applier._linkedin_identity = "rucha@example.com"
    messages: list[str] = []
    applier.progress_callback = messages.append

    with patch.object(linkedin_mod, "legacy_linkedin_cookie_path", return_value=legacy_cookie_path):
        injected = await applier._inject_scraper_cookies()

    assert injected is True
    cookies = applier._bm._context.add_cookies.await_args.args[0]
    by_name = {cookie["name"]: cookie for cookie in cookies}
    assert by_name["li_at"]["value"] == "legacy-li-at"
    assert any(
        "Falling back to legacy LinkedIn cookies for the same account" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_inject_scraper_cookies_refuses_legacy_cookie_file_without_matching_owner(tmp_path):
    scoped_cookie_path = tmp_path / "scoped_cookies.json"
    scoped_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {"lang": "en-us"},
            }
        ),
        encoding="utf-8",
    )
    legacy_cookie_path = tmp_path / "legacy_cookies.json"
    legacy_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "akshay@example.com",
                "cookies": {
                    "li_at": "legacy-li-at",
                    "JSESSIONID": '"ajax:456"',
                },
            }
        ),
        encoding="utf-8",
    )

    applier = make_applier()
    applier._bm = MagicMock()
    applier._bm._context = MagicMock()
    applier._bm._context.add_cookies = AsyncMock()
    applier._ensure_linkedin_state = MagicMock()
    applier._cookie_path = scoped_cookie_path
    applier._linkedin_identity = "rucha@example.com"

    with patch.object(linkedin_mod, "legacy_linkedin_cookie_path", return_value=legacy_cookie_path):
        injected = await applier._inject_scraper_cookies()

    assert injected is False
    applier._bm._context.add_cookies.assert_not_awaited()


def test_ensure_linkedin_state_scopes_paths_to_profile_credentials():
    profile = make_profile(
        job_board_accounts=JobBoardAccounts(
            linkedin=JobBoardCredentials(
                username="rucha@example.com",
                password="secret",
            )
        )
    )
    applier = make_applier(profile)

    applier._ensure_linkedin_state()

    cookie_path = str(applier._cookie_path).replace("\\", "/")
    session_dir = str(applier._session_dir).replace("\\", "/")
    assert "data/linkedin/" in cookie_path
    assert "rucha-example-com" in cookie_path
    assert cookie_path.endswith("/cookies.json")
    assert session_dir.endswith("/applier_session")


@pytest.mark.asyncio
async def test_is_logged_in_rejects_authwall_job_pages():
    applier = make_applier()
    page = MagicMock()
    page.url = "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F123"
    empty_locator = MagicMock()
    empty_locator.count = AsyncMock(return_value=0)
    page.locator.return_value = empty_locator

    assert await applier._is_logged_in(page) is False


def test_modal_descendants_scopes_each_modal_root():
    scoped = LinkedInApplier._modal_descendants("input[type='text']")
    assert scoped == (
        "div.jobs-easy-apply-modal input[type='text'], "
        "div.jobs-apply-modal input[type='text'], "
        "div[role='dialog'] input[type='text']"
    )


# ── Tests: _infer_value_from_label ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_checkbox_state_falls_back_to_dom_toggle_when_clicks_are_blocked():
    applier = make_applier()
    checkbox = _FakeCheckbox(checked=False)

    await applier._set_checkbox_state(checkbox, True, label="Top choice")

    assert checkbox.checked is True
    assert ("check", 2_000, False) in checkbox.calls
    assert ("check", 2_000, True) in checkbox.calls
    assert ("evaluate", True) in checkbox.calls


@pytest.mark.asyncio
async def test_set_radio_state_falls_back_to_label_click_when_input_is_blocked():
    applier = make_applier()
    radio = _FakeRadio(checked=False)
    label = _FakeLabel(radio)

    await applier._set_radio_state(
        radio,
        label="Will you now or in the future require sponsorship for employment visa status?",
        option_label="Yes",
        label_el=label,
    )

    assert radio.checked is True
    assert ("check", 2_000, False) in radio.calls
    assert ("click", 2_000, True) in label.calls


@pytest.mark.asyncio
async def test_click_modal_button_falls_back_to_forced_click_after_overlay_interception():
    applier = make_applier()
    applier._dismiss_prompts = AsyncMock()
    applier._dismiss_save_application_prompt = AsyncMock(return_value=False)
    applier._dismiss_autocomplete_overlays = AsyncMock()
    button = _FakeButton()
    page = MagicMock()

    await applier._click_modal_button(page, button, "next step")

    assert ("click", 2_000, False) in button.calls
    assert ("click", 2_000, True) in button.calls
    applier._dismiss_prompts.assert_awaited_once()
    assert applier._dismiss_save_application_prompt.await_count == 2
    assert applier._dismiss_autocomplete_overlays.await_count == 2


@pytest.mark.asyncio
async def test_dismiss_autocomplete_overlays_clicks_first_suggestion():
    applier = make_applier()
    page = _FakePageWithOverlay(visible=True)

    await applier._dismiss_autocomplete_overlays(page)

    assert page.overlay.clicks == [(2_000, True)]
    assert page.evaluations == []


@pytest.mark.asyncio
async def test_dismiss_prompts_does_not_click_easy_apply_modal_dismiss_button():
    applier = make_applier()
    page = _FakePromptPage(skip_click=True)

    await applier._dismiss_prompts(page)

    assert page.dismiss.clicks == []


@pytest.mark.asyncio
async def test_dismiss_save_application_prompt_closes_confirmation_dialog():
    applier = make_applier()
    page = _FakeSaveDialogPage(visible=True)

    closed = await applier._dismiss_save_application_prompt(page)

    assert closed is True
    assert page.dialog.close_button.clicks == [(2_000, True)]
    assert page.dialog.visible is False


def test_prepare_text_input_value_coerces_ai_text_for_numeric_experience_question():
    applier = make_applier()
    job = make_job()

    value = applier._prepare_text_input_value(
        "How many years experience do you have as a Data Scientist?",
        "6 years",
        field_type="number",
        input_type="number",
        job=job,
    )

    assert value == "6"


def test_prepare_text_input_value_uses_job_salary_for_numeric_salary_question():
    applier = make_applier()
    job = make_job(salary_min=80000, salary_max=100000)

    value = applier._prepare_text_input_value(
        "What is your salary expectation?",
        "Competitive, based on role and responsibilities",
        field_type="number",
        input_type="number",
        job=job,
    )

    assert value == "90000"


def test_prepare_text_input_value_uses_profile_city_when_ai_returns_placeholder_location():
    profile = make_profile(address=Address(city="Dublin", state="Dublin", zip_code="D02", country="IE"))
    applier = make_applier(profile)
    job = make_job()

    value = applier._prepare_text_input_value(
        "Location (city)",
        "Not specified",
        field_type="text",
        input_type="text",
        job=job,
    )

    assert value == "Dublin"


def test_is_non_resume_upload_field_detects_photo_context():
    assert LinkedInApplier._is_non_resume_upload_field("", "photo profile picture") is True


def test_is_resume_upload_field_detects_document_accept_types():
    assert LinkedInApplier._is_resume_upload_field("application/pdf,.docx", "upload file") is True


@pytest.mark.asyncio
async def test_find_resume_upload_input_skips_photo_field_and_uses_resume_field():
    applier = make_applier()
    progress_messages: list[str] = []
    applier.progress_callback = progress_messages.append
    photo_input = _FakeFileInput(accept="image/png,image/jpeg", name="photo")
    resume_input = _FakeFileInput(accept="application/pdf,.docx", name="resume")
    page = _FakeResumePage([photo_input, resume_input])
    applier._describe_upload_field = AsyncMock(
        side_effect=[
            ("Photo", "image/png,image/jpeg", "photo image"),
            ("Resume", "application/pdf,.docx", "resume document"),
        ]
    )

    found = await applier._find_resume_upload_input(page)

    assert found is resume_input
    assert progress_messages == ["[LinkedIn][Resume] Skipping non-resume upload field: Photo"]


@pytest.mark.asyncio
async def test_handle_resume_upload_skips_photo_only_steps(tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")
    profile = make_profile(resume_path=resume_path)
    applier = make_applier(profile)
    page = _FakeResumePage([_FakeFileInput(accept="image/png,image/jpeg", name="photo")])
    applier._find_resume_upload_input = AsyncMock(return_value=None)

    await applier._handle_resume_upload(page)

    applier._find_resume_upload_input.assert_awaited_once_with(page)
    assert page.inputs[0].uploads == []


@pytest.mark.asyncio
async def test_handle_resume_upload_uses_document_field(tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")
    profile = make_profile(resume_path=resume_path)
    applier = make_applier(profile)
    progress_messages: list[str] = []
    applier.progress_callback = progress_messages.append
    resume_input = _FakeFileInput(accept="application/pdf,.docx", name="resume")
    page = _FakeResumePage([resume_input])
    applier._find_resume_upload_input = AsyncMock(return_value=resume_input)

    await applier._handle_resume_upload(page)

    assert resume_input.uploads == [str(resume_path)]
    assert any("Uploaded resume: resume.pdf" in message for message in progress_messages)
    assert page.wait_calls


@pytest.mark.asyncio
async def test_resolve_answer_learns_inferred_answers_for_future_runs():
    applier = make_applier()

    answer, source = await applier._resolve_answer("Phone number", "text")

    assert answer == "+1-555-123-4567"
    assert source == "inferred"
    assert applier.profile.custom_answers["Phone number"] == "+1-555-123-4567"
    assert applier._learned_answers["Phone number"] == "+1-555-123-4567"


@pytest.mark.asyncio
async def test_resolve_answer_uses_ai_resolver_and_saves_exact_question():
    profile = make_profile(custom_answers={"authorized to work": "Yes"})
    applier = make_applier(profile)
    applier.answer_resolver = AsyncMock(return_value={"Are you open to relocation?": "No"})

    saved_answer, saved_source = await applier._resolve_answer(
        "Are you legally authorized to work in the US?",
        "radio",
        options=["Yes", "No"],
    )
    ai_answer, ai_source = await applier._resolve_answer("Are you open to relocation?", "checkbox")

    assert saved_answer == "Yes"
    assert saved_source == "saved"
    assert applier.profile.custom_answers["Are you legally authorized to work in the US?"] == "Yes"
    assert ai_answer == "No"
    assert ai_source == "ai"
    assert applier.profile.custom_answers["Are you open to relocation?"] == "No"


@pytest.mark.asyncio
async def test_resolve_answer_matches_ai_answers_against_normalized_question_key():
    applier = make_applier()
    applier.answer_resolver = AsyncMock(return_value={"Are you open to occasional travel?": "Yes"})

    answer, source = await applier._resolve_answer(
        "Are you open to occasional travel?\nAre you open to occasional travel?\nRequired",
        "radio",
        options=["Yes", "No"],
    )

    assert answer == "Yes"
    assert source == "ai"
    assert applier.profile.custom_answers["Are you open to occasional travel?"] == "Yes"


@pytest.mark.asyncio
async def test_resolve_answer_infers_commute_and_hybrid_questions_from_preferences():
    applier = make_applier()

    commute_answer, commute_source = await applier._resolve_answer(
        "Are you comfortable commuting to this job's location?\n"
        "Are you comfortable commuting to this job's location?\nRequired",
        "radio",
        options=["Yes", "No"],
    )
    hybrid_answer, hybrid_source = await applier._resolve_answer(
        "Are you comfortable working in a hybrid setting?\n"
        "Are you comfortable working in a hybrid setting?\nRequired",
        "radio",
        options=["Yes", "No"],
    )

    assert commute_answer == "Yes"
    assert commute_source == "inferred"
    assert hybrid_answer == "Yes"
    assert hybrid_source == "inferred"
    assert applier.profile.custom_answers["Are you comfortable commuting to this job's location?"] == "Yes"
    assert applier.profile.custom_answers["Are you comfortable working in a hybrid setting?"] == "Yes"


def test_match_answer_to_options_maps_yes_no_semantics_across_localized_options():
    applier = make_applier()
    assert applier._match_answer_to_options("Yes", ["Sí", "No"]) == "Sí"
    assert applier._match_answer_to_options("No", ["Sí", "No"]) == "No"


def test_mark_unanswered_stores_normalized_prompt_metadata():
    applier = make_applier()

    applier._mark_unanswered(
        "Are you comfortable working in a hybrid setting?\n"
        "Are you comfortable working in a hybrid setting?\nRequired",
        "radio",
        options=["Yes", "No"],
    )

    prompt = applier._unknown_question_prompts["Are you comfortable working in a hybrid setting?"]
    assert prompt.field_type == "radio"
    assert prompt.options == ["Yes", "No"]


def test_record_prefilled_answer_logs_visible_question_and_answer():
    applier = make_applier()
    messages: list[str] = []
    applier.progress_callback = messages.append

    applier._record_prefilled_answer("Email address", "jane@example.com", "text")

    assert len(applier._answered_questions) == 1
    assert applier._answered_questions[0].question == "Email address"
    assert applier._answered_questions[0].answer == "jane@example.com"
    assert applier._answered_questions[0].source == "prefilled"
    assert any(
        "[LinkedIn][Question][prefilled] Email address -> jane@example.com" in message
        for message in messages
    )


def test_record_answer_logs_each_question_only_once_per_application():
    applier = make_applier()
    messages: list[str] = []
    applier.progress_callback = messages.append

    applier._record_answer("Mark job as a top choice", "Yes", "ai", "checkbox")
    applier._record_prefilled_answer("Mark job as a top choice", "Yes", "checkbox")

    assert len(applier._answered_questions) == 1
    assert applier._answered_questions[0].question == "Mark job as a top choice"
    assert applier._answered_questions[0].answer == "Yes"
    assert sum(
        1
        for message in messages
        if "[LinkedIn][Question][ai] Mark job as a top choice -> Yes" in message
    ) == 1


def test_get_prefill_override_value_replaces_stale_prefilled_email():
    profile = make_profile(
        first_name="Rucha",
        last_name="Varma",
        email="rucha@example.com",
    )
    applier = make_applier(profile)

    assert (
        applier._get_prefill_override_value(
            "Email address",
            "akshay@example.com",
            field_type="text",
        )
        == "rucha@example.com"
    )
    assert (
        applier._get_prefill_override_value(
            "First name",
            "Akshay",
            field_type="text",
        )
        == "Rucha"
    )


def test_get_prefill_override_value_maps_phone_country_code_select_to_profile_country():
    profile = make_profile(
        address=Address(city="Frankfurt am Main", country="Germany"),
        phone="+49 17669099987",
    )
    applier = make_applier(profile)

    override = applier._get_prefill_override_value(
        "Phone country code",
        "Ireland (+353)",
        field_type="select",
        options=["Ireland (+353)", "Germany (+49)"],
    )

    assert override == "Germany (+49)"


@pytest.mark.asyncio
async def test_resolve_answer_does_not_emit_duplicate_request_progress_logs():
    applier = make_applier()
    applier.answer_resolver = AsyncMock(
        return_value={"Will you now or in the future require sponsorship for employment visa status?": "No"}
    )
    messages: list[str] = []
    applier.progress_callback = messages.append

    answer, source = await applier._resolve_answer(
        "Will you now or in the future require sponsorship for employment visa status?",
        "radio",
        options=["Yes", "No"],
    )

    assert answer == "No"
    assert source == "ai"
    assert messages == []


class TestInferValueFromLabel:
    def test_first_name(self):
        applier = make_applier()
        assert applier._infer_value_from_label("First name") == "Jane"

    def test_last_name(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Last Name") == "Doe"

    def test_email(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Email address") == "jane@example.com"

    def test_phone(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Phone number") == "+1-555-123-4567"

    def test_phone_country_code(self):
        profile = make_profile(address=Address(city="Frankfurt am Main", country="Germany"))
        applier = make_applier(profile)
        assert applier._infer_value_from_label("Phone country code") == "Germany"

    def test_linkedin_url(self):
        applier = make_applier()
        assert applier._infer_value_from_label("LinkedIn profile") == "https://linkedin.com/in/janedoe"

    def test_city(self):
        applier = make_applier()
        assert applier._infer_value_from_label("City") == "San Francisco"

    def test_years_experience(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Years of experience") == "7"

    def test_unknown_label_returns_none(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Favourite colour") is None


# ── Tests: _answer_for_label ─────────────────────────────────────────────────

class TestAnswerForLabel:
    def test_exact_key_match(self):
        applier = make_applier()
        result = applier._answer_for_label("Are you legally authorized to work in the US?")
        assert result == "Yes"

    def test_partial_match(self):
        applier = make_applier()
        result = applier._answer_for_label("Will you require sponsorship?")
        assert result == "No"

    def test_no_match_returns_none(self):
        applier = make_applier()
        result = applier._answer_for_label("Favourite ice cream flavour")
        assert result is None


# ── Tests: _build_cover_letter ───────────────────────────────────────────────

class TestBuildCoverLetter:
    def test_includes_job_title_and_company(self):
        applier = make_applier()
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Senior Python Engineer" in cl
        assert "Acme Corp" in cl

    def test_includes_applicant_name(self):
        applier = make_applier()
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Jane" in cl
        assert "Doe" in cl

    def test_uses_template_if_path_provided(self, tmp_path):
        template = tmp_path / "cover_letter.txt"
        template.write_text(
            "Dear Hiring Team,\n\nI want to join {company} as {job_title}.\n\n{first_name} {last_name}"
        )
        profile = make_profile(cover_letter_template_path=template)
        applier = make_applier(profile)
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Acme Corp" in cl
        assert "Senior Python Engineer" in cl
        assert "Jane Doe" in cl
