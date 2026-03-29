"""Playwright browser lifecycle helpers shared by all appliers."""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from src.config import settings

# ── Stealth init script ────────────────────────────────────────────────────────
# Patches multiple navigator/window properties that headless Chrome exposes.
_STEALTH_JS = """
() => {
    const uaDataBrands = [
        { brand: 'Not A(Brand', version: '99' },
        { brand: 'Chromium', version: '145' },
        { brand: 'Google Chrome', version: '145' },
    ];
    const fullVersionList = [
        { brand: 'Not A(Brand', version: '99.0.0.0' },
        { brand: 'Chromium', version: '145.0.0.0' },
        { brand: 'Google Chrome', version: '145.0.0.0' },
    ];

    // Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Spoof plugins list (headless has none by default)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Spoof languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    Object.defineProperty(navigator, 'platform', {
        get: () => 'Win32',
    });

    Object.defineProperty(navigator, 'vendor', {
        get: () => 'Google Inc.',
    });

    // Mask headless Chrome in userAgent string
    const originalUA = navigator.userAgent;
    Object.defineProperty(navigator, 'userAgent', {
        get: () => originalUA.replace('HeadlessChrome', 'Chrome'),
    });

    const uaData = {
        brands: uaDataBrands,
        mobile: false,
        platform: 'Windows',
        getHighEntropyValues: async (hints) => {
            const values = {
                architecture: 'x86',
                bitness: '64',
                brands: uaDataBrands,
                fullVersionList,
                mobile: false,
                model: '',
                platform: 'Windows',
                platformVersion: '10.0.0',
                uaFullVersion: '145.0.0.0',
                wow64: false,
            };
            return Object.fromEntries(hints.map((hint) => [hint, values[hint]]));
        },
        toJSON() {
            return {
                brands: uaDataBrands,
                mobile: false,
                platform: 'Windows',
            };
        },
    };
    Object.defineProperty(navigator, 'userAgentData', {
        get: () => uaData,
    });

    // Prevent detection via chrome.runtime
    window.chrome = { runtime: {} };

    // Spoof permissions query (headless returns 'denied' for notifications)
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters);
}
"""

# Realistic Chrome user-agent — shared by both persistent and ephemeral contexts
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_EXTRA_HEADERS = {
    "sec-ch-ua": '"Not A(Brand";v="99", "Chromium";v="145", "Google Chrome";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Realistic viewport sizes to randomise across
_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]


class BrowserManager:
    """Manages a single Playwright Chromium browser instance.

    Includes stealth measures to reduce bot-detection fingerprint:
    - Randomised viewport
    - navigator.webdriver patch + other property spoofs
    - Consistent Chrome user-agent (no HeadlessChrome leak)
    - Human-like typing and click delays (via ``human_type`` / ``human_click``)

    Usage::

        async with BrowserManager() as bm:
            page = await bm.new_page()
            await page.goto("https://example.com")
            await bm.human_type(page.locator("input"), "hello world")
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-extensions",
            "--disable-gpu",
            "--window-size=1440,900",
        ]
        viewport = random.choice(_VIEWPORTS)

        if self.user_data_dir:
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                args=launch_args,
                viewport=viewport,
                locale="en-US",
                timezone_id="America/New_York",
                # Explicit UA prevents the "HeadlessChrome" string from leaking
                # in the very first request before the page-level stealth JS runs.
                user_agent=_UA,
                ignore_https_errors=True,
                accept_downloads=True,
            )
            # Apply stealth at context level so it covers every document,
            # including iframes and popups (belt-and-suspenders with new_page() below).
            await self._context.add_init_script(_STEALTH_JS)
            await self._context.set_extra_http_headers(_EXTRA_HEADERS)
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=launch_args,
            )
            self._context = await self._browser.new_context(
                viewport=viewport,
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=_UA,
                ignore_https_errors=True,
                accept_downloads=True,
                extra_http_headers=_EXTRA_HEADERS,
            )

        self._context.set_default_timeout(self.timeout_ms)
        logger.debug(f"Browser started (headless={self.headless}, viewport={viewport})")

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.debug("Browser stopped")

    # ------------------------------------------------------------------
    # Page factory
    # ------------------------------------------------------------------

    async def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("BrowserManager not started — use async with")
        page = await self._context.new_page()
        # Page-level stealth as belt-and-suspenders (context-level set in start())
        await page.add_init_script(_STEALTH_JS)
        return page

    # ------------------------------------------------------------------
    # Human-like interaction helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def human_type(locator, text: str, wpm: int = 60) -> None:
        """Type *text* with per-character delays that mimic human typing speed.

        *wpm* — words per minute; 60 wpm ≈ 5 chars/s ≈ 200 ms/char average.
        Characters-per-second = wpm * 5 / 60.
        """
        if not text:
            return
        avg_delay = 60 / (wpm * 5)  # seconds per character
        for char in text:
            await locator.type(char, delay=0)
            jitter = random.gauss(avg_delay, avg_delay * 0.3)
            await asyncio.sleep(max(0.03, jitter))

    @staticmethod
    async def human_click(locator, page: Page) -> None:
        """Move mouse to element naturally then click."""
        box = await locator.bounding_box()
        if box:
            # Land somewhere inside the element, not always dead-center
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.15))
        await locator.click()

    @staticmethod
    async def human_pause(min_s: float = 0.5, max_s: float = 1.5) -> None:
        """Sleep a random human-like duration between *min_s* and *max_s* seconds."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    async def screenshot(self, page: Page, name: str) -> Path | None:
        """Save a screenshot to the configured screenshots directory."""
        dest = Path(settings.screenshots_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{name}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            logger.debug(f"Screenshot saved: {path}")
        except Exception as exc:
            logger.debug(f"Screenshot failed ({name}): {exc}")
            return None
        return path
