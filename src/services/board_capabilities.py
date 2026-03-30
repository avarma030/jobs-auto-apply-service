from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoardCapability:
    slug: str
    label: str
    scrape_supported: bool
    apply_supported: bool
    production_ready: bool
    status: str


_BOARD_CAPABILITIES: tuple[BoardCapability, ...] = (
    BoardCapability(
        slug="linkedin",
        label="LinkedIn",
        scrape_supported=True,
        apply_supported=True,
        production_ready=True,
        status="production",
    ),
    BoardCapability(
        slug="indeed",
        label="Indeed",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="glassdoor",
        label="Glassdoor",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="ziprecruiter",
        label="ZipRecruiter",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="dice",
        label="Dice",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="monster",
        label="Monster",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="lever",
        label="Lever",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="greenhouse",
        label="Greenhouse",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
    BoardCapability(
        slug="workday",
        label="Workday",
        scrape_supported=False,
        apply_supported=False,
        production_ready=False,
        status="planned",
    ),
)


def all_board_capabilities() -> list[BoardCapability]:
    return list(_BOARD_CAPABILITIES)


def supported_board_slugs() -> list[str]:
    return [board.slug for board in _BOARD_CAPABILITIES if board.production_ready]


def normalize_enabled_boards(boards: list[str] | None) -> list[str]:
    supported = set(supported_board_slugs())
    normalized = [
        board.strip().lower()
        for board in (boards or [])
        if board and board.strip().lower() in supported
    ]
    if normalized:
        return list(dict.fromkeys(normalized))
    return supported_board_slugs()


def unsupported_requested_boards(boards: list[str] | None) -> list[str]:
    supported = set(supported_board_slugs())
    requested = [
        board.strip().lower()
        for board in (boards or [])
        if board and board.strip()
    ]
    return [board for board in requested if board not in supported]
