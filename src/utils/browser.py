"""Playwright browser lifecycle helpers shared by all appliers."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from src.config import settings


class BrowserManager:
    """Manages a single Playwright Chromium browser instance.

    Usage::

        async with BrowserManager() as bm:
            page = await bm.new_page()
            await page.goto("https://example.com")
    """

    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        user_data_dir: Path | None = None,
    ):
        self.headless = headless if headless is not None else settings.headless_browser
        self.timeout_ms = (timeout_ms or settings.browser_timeout_seconds) * 1000
        self.user_data_dir = user_data_dir

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]

        if self.user_data_dir:
            # Persistent context — keeps cookies/session across runs
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                args=launch_args,
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=launch_args,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )

        self._context.set_default_timeout(self.timeout_ms)
        logger.debug(f"Browser started (headless={self.headless})")

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.debug("Browser stopped")

    async def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("BrowserManager not started — use async with")
        page = await self._context.new_page()
        # Make automation less detectable
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return page

    async def screenshot(self, page: Page, name: str) -> Path | None:
        """Save a screenshot to the configured screenshots directory."""
        if not settings.screenshot_on_failure:
            return None
        dest = Path(settings.screenshots_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.debug(f"Screenshot saved: {path}")
        return path
