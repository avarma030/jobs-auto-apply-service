"""Playwright browser lifecycle helpers shared by browser-driven appliers."""
from __future__ import annotations

import asyncio
import importlib
import random
from pathlib import Path
from typing import Any

from loguru import logger

from src.config import settings

_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
}
"""

_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
]


class BrowserManager:
    """Manage a persistent Playwright Chromium browser/context."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        user_data_dir: Path | None = None,
    ) -> None:
        self.headless = settings.headless_browser if headless is None else headless
        self.timeout_ms = (timeout_ms or settings.browser_timeout_seconds) * 1000
        self.user_data_dir = user_data_dir
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._context is not None:
            return

        async_api = self._load_playwright_api()
        self._playwright = await async_api.async_playwright().start()
        viewport = random.choice(_VIEWPORTS)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-infobars",
            "--window-size=1440,900",
        ]

        if self.user_data_dir is not None:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                args=args,
                viewport=viewport,
                locale="en-US",
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=args,
            )
            self._context = await self._browser.new_context(
                viewport=viewport,
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )

        self._context.set_default_timeout(self.timeout_ms)
        logger.debug(f"Browser started (headless={self.headless}, viewport={viewport})")

    async def stop(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None
        logger.debug("Browser stopped")

    async def new_page(self) -> Any:
        if self._context is None:
            raise RuntimeError("BrowserManager not started")
        page = await self._context.new_page()
        await page.add_init_script(_STEALTH_JS)
        return page

    async def screenshot(self, page: Any, name: str) -> Path | None:
        destination = Path(settings.screenshots_dir)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{name}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            logger.debug(f"Screenshot failed for {name}: {exc}")
            return None
        return path

    @staticmethod
    async def human_type(locator: Any, text: str, *, wpm: int = 70) -> None:
        if not text:
            return
        average_delay = 60 / max(wpm * 5, 1)
        for character in text:
            await locator.type(character, delay=0)
            await asyncio.sleep(max(0.02, random.gauss(average_delay, average_delay * 0.25)))

    @staticmethod
    async def human_click(locator: Any, page: Any) -> None:
        try:
            box = await locator.bounding_box()
        except Exception:
            box = None
        if box:
            x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await page.mouse.move(x, y, steps=random.randint(4, 12))
            await asyncio.sleep(random.uniform(0.04, 0.12))
        await locator.click()

    @staticmethod
    async def human_pause(min_seconds: float = 0.3, max_seconds: float = 1.0) -> None:
        await asyncio.sleep(random.uniform(min_seconds, max_seconds))

    @staticmethod
    def _load_playwright_api() -> Any:
        try:
            return importlib.import_module("playwright.async_api")
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed in this environment. "
                "Install it and run `playwright install chromium` before using LinkedIn automation."
            ) from exc
