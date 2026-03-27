from __future__ import annotations

from datetime import datetime

from src.dashboard import DashboardQuery, format_posted_at, parse_dashboard_query, render_dashboard_page, split_keywords
from src.models import Job


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
            "keywords": ["project manager, delivery lead"],
            "location": ["Ireland"],
            "posted_within": ["7d"],
            "remote_only": ["on"],
            "limit": ["80"],
        }
    )

    assert query == DashboardQuery(
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


def test_render_dashboard_page_shows_polished_result_cards() -> None:
    job = Job(
        title="Senior Project Manager",
        company="Acme Delivery",
        location="Dublin, Ireland",
        description="Lead cross-functional delivery across a complex transformation programme.",
        url="https://example.com/jobs/123",
        source_board="linkedin",
        external_id="123",
        job_type="contract",
        experience_level="senior",
        posted_at=datetime(2026, 3, 27),
        easy_apply=True,
        tags=["Transformation", "Stakeholder Management"],
    )

    html = render_dashboard_page(
        query=DashboardQuery(
            keywords_raw="project manager",
            location="Ireland",
            posted_within="24h",
            remote_only=False,
            limit=10,
        ),
        results=[job],
        duration_seconds=1.23,
    )

    assert "Project manager in Ireland" in html
    assert "Copy search summary" in html
    assert "Copy link" in html
    assert "Open Apply Link" in html
    assert "Next best move" in html
    assert "Open top match" in html
    assert "Top result" in html
    assert "Found 1 LinkedIn result(s) in 1.2s." in html


def test_render_dashboard_page_shows_empty_state() -> None:
    html = render_dashboard_page()

    assert "Your live results will land here." in html
    assert "Recent searches" in html
    assert "Shortcuts" in html
    assert "Ctrl + Enter" in html
