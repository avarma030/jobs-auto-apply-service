from __future__ import annotations

from datetime import datetime

import pytest

from src import dashboard as dashboard_module
from src.dashboard import DashboardQuery, format_posted_at, parse_dashboard_query, render_dashboard_page, run_dashboard_search, split_keywords
from src.models import (
    ApplicationPackage,
    ApplicationRoute,
    AutopilotRun,
    AutomationState,
    Job,
    JobAutomationResult,
)


def build_result(
    *,
    board: str = "linkedin",
    route: ApplicationRoute = ApplicationRoute.LINKEDIN_EASY_APPLY,
    state: AutomationState = AutomationState.QUEUED,
    handler_name: str = "LinkedInApplier",
) -> JobAutomationResult:
    job = Job(
        title="Senior Project Manager",
        company="Acme Delivery",
        location="Dublin, Ireland",
        description="Lead cross-functional delivery across a complex transformation programme.",
        url="https://example.com/jobs/123",
        source_board=board,
        external_id="123",
        job_type="contract",
        experience_level="senior",
        posted_at=datetime(2026, 3, 27),
        easy_apply=route == ApplicationRoute.LINKEDIN_EASY_APPLY,
    )
    return JobAutomationResult(
        job=job,
        compatibility_score=92,
        compatibility_reasons=["25% search alignment", "37% keyword overlap"],
        route=route,
        handler_name=handler_name,
        package=ApplicationPackage(
            resume_path=None,
            cover_letter_path=None,
            resume_preview="Tailored resume preview",
            cover_letter_text="Tailored cover letter",
            ats_score=95,
            matched_keywords=["project", "manager", "delivery"],
            added_keywords=["stakeholder"],
        ),
        automation_state=state,
        auto_apply_message=f"Tailored application package is ready and routed to {handler_name}.",
    )


def test_split_keywords_preserves_single_phrase() -> None:
    assert split_keywords("project manager") == ["project manager"]


def test_split_keywords_handles_commas_and_newlines() -> None:
    assert split_keywords("project manager,\nprogram manager, delivery lead") == [
        "project manager",
        "program manager",
        "delivery lead",
    ]


def test_parse_dashboard_query_normalizes_form_values() -> None:
    query = parse_dashboard_query(
        {
            "board": ["workday"],
            "keywords": ["project manager, delivery lead"],
            "location": ["Ireland"],
            "posted_within": ["7d"],
            "remote_only": ["on"],
            "limit": ["80"],
        }
    )

    assert query == DashboardQuery(
        board="workday",
        keywords_raw="project manager, delivery lead",
        location="Ireland",
        posted_within="7d",
        remote_only=True,
        limit=25,
    )
    assert query.to_search_filter().keywords == ["project manager", "delivery lead"]
    assert query.to_search_filter().max_age_days == 7


def test_format_posted_at_handles_date_only() -> None:
    assert format_posted_at(datetime(2026, 3, 27)) == "2026-03-27"


def test_render_dashboard_page_shows_autopilot_result_cards() -> None:
    run = AutopilotRun(
        board="linkedin",
        results=[build_result(state=AutomationState.APPLIED)],
        total_scraped=4,
        filtered_out_count=2,
        auto_applied_count=1,
    )

    html = render_dashboard_page(
        query=DashboardQuery(
            keywords_raw="project manager",
            location="Ireland",
            posted_within="24h",
            remote_only=False,
            limit=10,
        ),
        run=run,
        duration_seconds=1.23,
    )

    assert "Search once. Let the application system take it from there." in html
    assert "Autopilot run complete" in html
    assert "Scraped 4 LinkedIn job(s), filtered out 2, kept 1 visible Easy Apply match(es), submitted 1, and held back 0" in html
    assert "Match 92%" in html
    assert "ATS 95%" in html
    assert "Applied via LinkedInApplier." in html
    assert "Run autopilot" in html
    assert "Copy search summary" in html


def test_render_dashboard_page_supports_workday_selection() -> None:
    run = AutopilotRun(
        board="workday",
        results=[
            build_result(
                board="workday",
                route=ApplicationRoute.WORKDAY,
                handler_name="WorkdayApplier",
            )
        ],
        total_scraped=3,
        queued_count=1,
    )

    html = render_dashboard_page(
        query=DashboardQuery(
            board="workday",
            keywords_raw="program manager",
            location="Ireland",
            posted_within="24h",
            remote_only=False,
            limit=10,
        ),
        run=run,
        duration_seconds=1.4,
    )

    assert 'option value="workday" selected' in html
    assert "Live Workday autopilot console" in html
    assert "Scraped 3 Workday job(s), filtered out 0, kept 1 visible shortlisted match(es), submitted 0, and held back 0" in html
    assert "Board: Workday" in html
    assert "Queued via WorkdayApplier." in html


def test_render_dashboard_page_shows_empty_state() -> None:
    html = render_dashboard_page()

    assert "Your autopilot shortlist will land here." in html
    assert "Recent searches" in html
    assert "Shortcuts" in html
    assert "Ctrl + Enter" in html


@pytest.mark.asyncio
async def test_run_dashboard_search_dispatches_to_autopilot(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = AutopilotRun(board="workday", total_scraped=2)

    class StubEngine:
        def __init__(self, profile, live_apply_routes) -> None:
            self.profile = profile
            self.live_apply_routes = live_apply_routes

        async def run_search(self, *, board, scraper_cls, search_filter, limit):
            assert board == "workday"
            assert scraper_cls is object
            assert search_filter.keywords == ["program manager"]
            assert search_filter.location == "Ireland"
            assert limit == 10
            assert self.live_apply_routes == set()
            return expected

    monkeypatch.setattr(dashboard_module, "AutopilotEngine", StubEngine)
    monkeypatch.setattr(dashboard_module, "load_profile", lambda _path: object())
    monkeypatch.setitem(dashboard_module.SCRAPER_REGISTRY, "workday", object)
    monkeypatch.setattr(
        dashboard_module.settings,
        "workday_tenant_urls",
        "https://acme.wd5.myworkdayjobs.com/en-US/Careers",
    )

    run = await run_dashboard_search(
        DashboardQuery(
            board="workday",
            keywords_raw="program manager",
            location="Ireland",
            posted_within="24h",
            remote_only=False,
            limit=10,
        )
    )

    assert run == expected
