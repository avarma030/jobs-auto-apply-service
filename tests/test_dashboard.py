from __future__ import annotations

from src.dashboard import DashboardQuery, format_posted_at, parse_dashboard_query, split_keywords


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
    from datetime import datetime

    assert format_posted_at(datetime(2026, 3, 27)) == "2026-03-27"
