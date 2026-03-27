from __future__ import annotations

from pathlib import Path

import pytest

import src.appliers.linkedin as linkedin_module
from src.appliers.linkedin import (
    _CHECKBOXES,
    _EASY_APPLY_BTN,
    _ERROR_MSG,
    _FILE_INPUT,
    _MODAL,
    _RADIOS,
    _RESUME_CARD,
    _SELECTS,
    _SUBMIT_BTN,
    _TEXT_INPUTS,
    _TEXTAREAS,
    LinkedInApplier,
)
from src.models import Address, ApplicationPackage, ApplicationStatus, Job, SocialLinks, UserProfile, WorkExperience


def build_profile() -> UserProfile:
    return UserProfile(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+353-1-555-0100",
        address=Address(city="Dublin", state="D", zip_code="D01", country="IE"),
        headline="Senior Project Manager",
        summary="Project and delivery leader with strong stakeholder and programme experience.",
        years_of_experience=8,
        social_links=SocialLinks(linkedin="https://linkedin.com/in/janedoe"),
        work_experience=[
            WorkExperience(
                company="Acme Delivery",
                title="Senior Project Manager",
                start_date="2021-01",
                description="Led agile delivery and programme execution.",
            )
        ],
        custom_answers={
            "Are you legally authorized to work in Ireland?": "Yes",
        },
    )


def build_job() -> Job:
    return Job(
        title="Senior Project Manager",
        company="Acme Delivery",
        location="Dublin, Ireland",
        description="Lead programme delivery and stakeholder management.",
        url="https://www.linkedin.com/jobs/view/123/",
        source_board="linkedin",
        external_id="123",
        easy_apply=True,
    )


class FakeElement:
    def __init__(
        self,
        *,
        text: str = "",
        value: str = "",
        visible: bool = True,
        enabled: bool = True,
        checked: bool = False,
        attributes: dict[str, str] | None = None,
        children: dict[str, list["FakeElement"]] | None = None,
    ) -> None:
        self.text = text
        self.value = value
        self.visible = visible
        self.enabled = enabled
        self.checked = checked
        self.attributes = attributes or {}
        self.children = children or {}
        self.clicked = False
        self.uploaded_files: list[str] = []

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def input_value(self) -> str:
        return self.value

    async def fill(self, value: str) -> None:
        self.value = value

    async def type(self, value: str, delay: int = 0) -> None:
        self.value += value

    async def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    async def inner_text(self) -> str:
        return self.text

    async def click(self) -> None:
        self.clicked = True

    async def set_input_files(self, value: str) -> None:
        self.uploaded_files.append(value)

    async def is_checked(self) -> bool:
        return self.checked

    async def check(self) -> None:
        self.checked = True

    async def uncheck(self) -> None:
        self.checked = False

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 0, "y": 0, "width": 10, "height": 10}

    async def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        self.value = label or value or self.value

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.children.get(selector, []))


class FakeLocator:
    def __init__(self, elements: list[FakeElement]) -> None:
        self.elements = elements

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.elements[:1])

    async def count(self) -> int:
        return len(self.elements)

    async def all(self) -> list[FakeElement]:
        return list(self.elements)

    async def is_visible(self) -> bool:
        return bool(self.elements) and await self.elements[0].is_visible()

    async def is_enabled(self) -> bool:
        return bool(self.elements) and await self.elements[0].is_enabled()

    async def inner_text(self) -> str:
        return await self.elements[0].inner_text() if self.elements else ""

    async def get_attribute(self, name: str) -> str | None:
        return await self.elements[0].get_attribute(name) if self.elements else None

    async def input_value(self) -> str:
        return await self.elements[0].input_value() if self.elements else ""

    async def fill(self, value: str) -> None:
        if self.elements:
            await self.elements[0].fill(value)

    async def type(self, value: str, delay: int = 0) -> None:
        if self.elements:
            await self.elements[0].type(value, delay=delay)

    async def click(self) -> None:
        if self.elements:
            await self.elements[0].click()

    async def set_input_files(self, value: str) -> None:
        if self.elements:
            await self.elements[0].set_input_files(value)

    async def is_checked(self) -> bool:
        return await self.elements[0].is_checked() if self.elements else False

    async def check(self) -> None:
        if self.elements:
            await self.elements[0].check()

    async def uncheck(self) -> None:
        if self.elements:
            await self.elements[0].uncheck()

    async def bounding_box(self) -> dict[str, float] | None:
        if self.elements:
            return await self.elements[0].bounding_box()
        return None

    async def select_option(self, *, label: str | None = None, value: str | None = None) -> None:
        if self.elements:
            await self.elements[0].select_option(label=label, value=value)


class FakeMouse:
    async def move(self, _x: float, _y: float, steps: int = 1) -> None:
        return None


class FakePage:
    def __init__(self, selectors: dict[str, list[FakeElement]] | None = None) -> None:
        self.selectors = selectors or {}
        self.url = ""
        self.mouse = FakeMouse()

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.selectors.get(selector, []))

    async def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        if selector not in self.selectors or not self.selectors[selector]:
            raise RuntimeError(f"missing selector {selector}")

    async def add_init_script(self, _script: str) -> None:
        return None


class FakeBrowserManager:
    async def screenshot(self, _page, _name: str) -> None:
        return None


@pytest.fixture(autouse=True)
def fast_browser_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant_pause(*_args, **_kwargs) -> None:
        return None

    async def instant_click(locator, _page) -> None:
        await locator.click()

    async def instant_type(locator, text: str, **_kwargs) -> None:
        await locator.fill(text)

    monkeypatch.setattr(linkedin_module._BM, "human_pause", instant_pause)
    monkeypatch.setattr(linkedin_module._BM, "human_click", instant_click)
    monkeypatch.setattr(linkedin_module._BM, "human_type", instant_type)


@pytest.mark.asyncio
async def test_linkedin_apply_requires_saved_session() -> None:
    applier = LinkedInApplier(build_profile())
    applier._session_error = "LinkedIn session not found or expired. Run `python main.py login linkedin`."

    result = await applier.apply(build_job())

    assert result.status == ApplicationStatus.SKIPPED
    assert "login linkedin" in result.message


@pytest.mark.asyncio
async def test_fill_modal_page_uses_profile_answers_and_tailored_resume(tmp_path: Path) -> None:
    applier = LinkedInApplier(build_profile())

    first_name = FakeElement(attributes={"id": "first-name", "required": "true"})
    file_input = FakeElement()
    page = FakePage(
        selectors={
            f"{_MODAL} {_TEXT_INPUTS}": [first_name],
            "label[for='first-name']": [FakeElement(text="First name")],
            f"{_MODAL} {_TEXTAREAS}": [],
            f"{_MODAL} {_SELECTS}": [],
            f"{_MODAL} {_RADIOS}": [],
            f"{_MODAL} {_CHECKBOXES}": [],
            f"{_MODAL} {_FILE_INPUT}": [file_input],
            _RESUME_CARD: [],
        }
    )
    resume_path = tmp_path / "resume.docx"
    resume_path.write_bytes(b"docx")
    package = ApplicationPackage(
        resume_path=resume_path,
        cover_letter_text="Tailored cover letter",
        ats_score=95,
    )

    issue = await applier._fill_modal_page(page, build_job(), package=package)

    assert issue is None
    assert first_name.value == "Jane"
    assert file_input.uploaded_files == [str(resume_path)]


@pytest.mark.asyncio
async def test_fill_modal_page_returns_manual_review_for_unknown_radio_question() -> None:
    applier = LinkedInApplier(build_profile())
    yes_option = FakeElement(attributes={"id": "q-yes", "value": "Yes"})
    no_option = FakeElement(attributes={"id": "q-no", "value": "No"})
    fieldset = FakeElement(
        children={
            "legend": [FakeElement(text="Do you have a PMP certification?")],
            "input[type='radio']:checked": [],
            "input[type='radio']": [yes_option, no_option],
        }
    )
    page = FakePage(
        selectors={
            f"{_MODAL} {_TEXT_INPUTS}": [],
            f"{_MODAL} {_TEXTAREAS}": [],
            f"{_MODAL} {_SELECTS}": [],
            f"{_MODAL} {_RADIOS}": [fieldset],
            f"{_MODAL} {_CHECKBOXES}": [],
            f"{_MODAL} {_FILE_INPUT}": [],
            _RESUME_CARD: [],
            "label[for='q-yes']": [FakeElement(text="Yes")],
            "label[for='q-no']": [FakeElement(text="No")],
        }
    )

    issue = await applier._fill_modal_page(page, build_job(), package=None)

    assert issue == "Manual review required for LinkedIn question: Do you have a PMP certification?"


@pytest.mark.asyncio
async def test_linkedin_easy_apply_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    applier = LinkedInApplier(build_profile())
    applier._bm = FakeBrowserManager()
    applier._page = FakePage(
        selectors={
            _EASY_APPLY_BTN: [FakeElement()],
            _MODAL: [FakeElement()],
            _SUBMIT_BTN: [FakeElement()],
            _ERROR_MSG: [],
        }
    )

    async def no_op_fill(_page, _job, *, package=None):
        return None

    async def no_op_prompts(_page):
        return None

    monkeypatch.setattr(applier, "_fill_modal_page", no_op_fill)
    monkeypatch.setattr(applier, "_dismiss_prompts", no_op_prompts)

    resume_path = tmp_path / "resume.docx"
    resume_path.write_bytes(b"docx")
    package = ApplicationPackage(
        resume_path=resume_path,
        cover_letter_text="Tailored cover letter",
        ats_score=95,
    )

    result = await applier.apply(build_job(), package=package)

    assert result.status == ApplicationStatus.APPLIED
    assert result.confirmation_id == "123"
