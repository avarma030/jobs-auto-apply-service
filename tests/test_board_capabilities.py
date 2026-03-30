from src.services.board_capabilities import (
    normalize_enabled_boards,
    supported_board_slugs,
    unsupported_requested_boards,
)


def test_supported_boards_are_linkedin_first_for_live_execution():
    assert supported_board_slugs() == ["linkedin"]


def test_normalize_enabled_boards_filters_non_production_boards_and_preserves_supported_values():
    normalized = normalize_enabled_boards(["linkedin", "indeed", "glassdoor", "linkedin"])

    assert normalized == ["linkedin"]


def test_normalize_enabled_boards_falls_back_to_supported_defaults():
    assert normalize_enabled_boards(["indeed", "glassdoor"]) == ["linkedin"]
    assert normalize_enabled_boards([]) == ["linkedin"]


def test_unsupported_requested_boards_identifies_non_production_requests():
    unsupported = unsupported_requested_boards(["linkedin", "indeed", "monster"])

    assert unsupported == ["indeed", "monster"]
