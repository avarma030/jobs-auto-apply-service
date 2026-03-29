"""LinkedIn job scraper.

Scraping strategy
-----------------
Hybrid Apify-style approach with Chrome TLS fingerprint impersonation:

1. Session warming — Playwright visits linkedin.com/jobs/search (or logs in if
   credentials are configured) using a stealth browser profile stored in
   ``data/.linkedin_scraper_session/``.  Extracts real session cookies.

2. Cookie-authenticated curl-cffi — cookies are injected into a curl-cffi
   AsyncSession configured with ``impersonate="chrome124"``, which reproduces
   Chrome's exact JA3 TLS fingerprint and HTTP/2 SETTINGS frame.  This prevents
   Akamai/PerimeterX from flagging us at the TLS layer.
   API endpoint: GET /jobs-guest/jobs/api/seeMoreJobPostings/search

3. Cookie persistence — saved to ``data/.linkedin_cookies.json``, reused up to
   4 hours before re-warming.

4. Re-warm on block — on HTTP 403/999/CAPTCHA the scraper re-warms and retries once.

5. Playwright fallback — if the guest API returns empty even after re-warm,
   the first page is scraped directly via Playwright.

Credentials in ``self.credentials`` are used both to log in during warming
(obtaining a trusted ``li_at`` cookie) and by the applier.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings
from src.models import ExperienceLevel, Job, JobSearchFilter, JobType, WorkMode
from src.scrapers.base import BaseScraper
from src.services.linkedin_state import (
    legacy_linkedin_cookie_path,
    linkedin_cookie_path,
    linkedin_session_dir,
    mask_linkedin_username,
)
from src.utils.browser import BrowserManager

# ── Constants ──────────────────────────────────────────────────────────────────

_GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
_JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
# Guest API for individual job postings — returns a static server-rendered HTML fragment.
# The /jobs/view/{id}/ URL returns a client-side-rendered React SPA shell with no content.
_JOB_POSTING_API_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
# LinkedIn's internal Voyager JSON API — returns rich structured data when authenticated.
# Requires li_at + JSESSIONID cookies; JSESSIONID value used as the csrf-token header.
# Response is a flat JSON object: {"description": {"text": "..."}, "employmentStatus": "...", ...}
_VOYAGER_JOB_URL = (
    "https://www.linkedin.com/voyager/api/jobs/jobPostings/{job_id}"
    "?decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebLightJobPosting-23"
)

# Cookie / session persistence
_COOKIE_MAX_AGE_SECONDS = 4 * 3600          # re-warm after 4 hours
_DETAIL_API_AUTHWALL_BACKOFF_SECONDS = 5 * 60


def _clear_session_dir(path: Path) -> None:
    """Delete and recreate a Playwright persistent profile dir to reset browser state.

    Called when the login form is not found — indicates LinkedIn showed a
    security checkpoint due to stale/flagged session state.  A clean dir
    forces LinkedIn to treat the next visit as a brand-new browser.
    """
    try:
        if path.exists():
            shutil.rmtree(str(path))
        path.mkdir(parents=True, exist_ok=True)
        logger.info(f"[LinkedIn] Reset browser profile dir: {path}")
    except Exception as exc:
        logger.warning(f"[LinkedIn] Could not reset browser profile dir {path}: {exc}")


# Full browser header suite — mirrors a real Chrome navigation request
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.linkedin.com/jobs/search/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

# LinkedIn filter codes
_WORK_TYPE_MAP = {
    WorkMode.REMOTE: "2",
    WorkMode.HYBRID: "3",
    WorkMode.ONSITE: "1",
}
_JOB_TYPE_MAP = {
    JobType.FULL_TIME: "F",
    JobType.PART_TIME: "P",
    JobType.CONTRACT: "C",
    JobType.TEMPORARY: "T",
    JobType.INTERNSHIP: "I",
}
_EXP_LEVEL_MAP = {
    ExperienceLevel.ENTRY: "2",
    ExperienceLevel.MID: "4",
    ExperienceLevel.SENIOR: "4",
    ExperienceLevel.LEAD: "5",
    ExperienceLevel.EXECUTIVE: "6",
}

_PAGE_SIZE = 25
_MAX_PAGES = 40  # 1 000 jobs per search

# Signals LinkedIn returned a login/CAPTCHA wall instead of job cards
_CAPTCHA_SIGNALS = (
    "authwall",
    "checkpoint/challenge",
    "linkedin.com/uas/login",
    "Sign in",
)


class _RateLimitError(Exception):
    """Raised on 429 so tenacity can retry with backoff."""


class LinkedInScraper(BaseScraper):
    """Scrapes job listings from LinkedIn via the public guest search endpoint."""

    board_name = "LinkedIn"
    board_slug = "linkedin"
    requires_auth = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        await super().setup()
        self._ua = UserAgent()
        self._proxies: list[str] = self._load_proxies()
        self._proxy_index = 0
        self._cookies: dict = {}
        self._detail_bm: BrowserManager | None = None  # persistent Playwright for detail pages
        self._detail_api_authwall_backoff_until = 0.0
        self._ensure_linkedin_state()

        # Load cached cookies — warm a fresh session if missing / stale
        cached = self._load_cookies()
        if cached:
            logger.info(f"[LinkedIn] Loaded {len(cached)} cached cookies from disk")
            self._cookies = cached
        else:
            self._cookies = await self._warm_session()
            if self._cookies:
                self._save_cookies(self._cookies)

        self._client = self._make_client()

    def _make_client(self, proxy: str | None = None) -> AsyncSession:
        """Build a curl-cffi session that impersonates Chrome's TLS fingerprint.

        Using curl-cffi instead of httpx is critical: LinkedIn's Akamai/PerimeterX
        bot detection checks the JA3 TLS fingerprint.  httpx's cipher suite order
        differs from Chrome and gets flagged immediately.  curl-cffi reproduces
        Chrome 124's exact JA3 hash and HTTP/2 SETTINGS frame.
        """
        return AsyncSession(
            impersonate="chrome124",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                **_BROWSER_HEADERS,
            },
            cookies=self._cookies,
            timeout=30,
            proxies={"https": proxy, "http": proxy} if proxy else None,
        )

    def _resolve_linkedin_credentials(self) -> tuple[str, str, str]:
        username = str(self.credentials.get("username") or "").strip()
        password = str(self.credentials.get("password") or "").strip()
        source = "profile"
        if (not username or not password) and settings.linkedin_email:
            username = settings.linkedin_email.strip()
            password = (settings.linkedin_password or "").strip()
            source = "environment"
        return username, password, source

    def _ensure_linkedin_state(self) -> None:
        username, password, source = self._resolve_linkedin_credentials()
        identity = username or "default"
        self._linkedin_username = username
        self._linkedin_password = password
        self._linkedin_identity = identity
        self._linkedin_credential_source = source
        self._cookie_path = linkedin_cookie_path(identity)
        self._session_dir = linkedin_session_dir(identity, "scraper")
        self._detail_session_dir = linkedin_session_dir(identity, "detail")

    def _load_proxies(self) -> list[str]:
        if not settings.use_proxies or not settings.proxy_list_path:
            return []
        path = Path(settings.proxy_list_path)
        if not path.exists():
            logger.warning(f"[LinkedIn] Proxy list not found: {path}")
            return []
        proxies = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        logger.info(f"[LinkedIn] Loaded {len(proxies)} proxies")
        return proxies

    def _next_proxy(self) -> str | None:
        if not self._proxies:
            return None
        proxy = self._proxies[self._proxy_index % len(self._proxies)]
        self._proxy_index += 1
        return proxy

    async def teardown(self) -> None:
        if hasattr(self, "_detail_bm") and self._detail_bm:
            try:
                await self._detail_bm.stop()
            except Exception as exc:
                logger.debug(f"[LinkedIn] Detail browser teardown: {exc}")
            self._detail_bm = None
        if hasattr(self, "_client"):
            await self._client.close()
        await super().teardown()

    # ------------------------------------------------------------------
    # Cookie / session helpers
    # ------------------------------------------------------------------

    def _load_cookies(self) -> dict | None:
        """Load cookies from disk.  Returns None if missing or older than 4 h."""
        self._ensure_linkedin_state()
        candidates: list[tuple[Path, str, str | None]] = [
            (self._cookie_path, "scoped", self._linkedin_identity),
            (legacy_linkedin_cookie_path(), "legacy", self._linkedin_identity),
        ]
        guest_cookies: dict | None = None
        for path, source, expected_owner in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
                age = time.time() - data.get("saved_at", 0)
                if age > _COOKIE_MAX_AGE_SECONDS:
                    logger.info(f"[LinkedIn] Cached cookies are {age / 3600:.1f}h old — re-warming")
                    continue
                cookie_owner = str(data.get("username") or "").strip().lower()
                if expected_owner:
                    expected = expected_owner.strip().lower()
                    if not cookie_owner:
                        logger.info(
                            f"[LinkedIn] Ignoring {source} cookie file without LinkedIn account metadata"
                        )
                        continue
                    if cookie_owner != expected:
                        logger.info(
                            "[LinkedIn] Ignoring cached cookies for a different LinkedIn account: "
                            f"{mask_linkedin_username(cookie_owner)}"
                        )
                        continue
                cookies = data.get("cookies", {})
                if self._has_authenticated_cookie(cookies):
                    if source == "legacy":
                        logger.warning(
                            "[LinkedIn] Falling back to legacy LinkedIn cookies for the same account "
                            "to preserve authenticated scraping"
                        )
                    return cookies
                if guest_cookies is None:
                    guest_cookies = cookies
            except Exception as exc:
                logger.warning(f"[LinkedIn] Failed to load cookies from {path}: {exc}")
        return guest_cookies

    def _save_cookies(self, cookies: dict) -> None:
        """Persist cookies with a timestamp."""
        self._ensure_linkedin_state()
        payload = {
            "saved_at": time.time(),
            "username": self._linkedin_identity,
            "cookies": cookies,
        }
        self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_path.write_text(json.dumps(payload))
        if self._has_authenticated_cookie(cookies):
            legacy_path = legacy_linkedin_cookie_path()
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(json.dumps(payload))
        logger.debug("[LinkedIn] Cookies saved to disk")

    @staticmethod
    def _has_authenticated_cookie(cookies: dict) -> bool:
        return bool(cookies.get("li_at"))

    @staticmethod
    def _is_guest_job_posting_url(url: str) -> bool:
        return url.startswith("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/")

    def has_authenticated_session(self) -> bool:
        return self._has_authenticated_cookie(getattr(self, "_cookies", {}))

    async def _warm_session(self) -> dict:
        """
        Launch a stealth Playwright browser against linkedin.com/jobs/search,
        scroll briefly to appear human, and extract all response cookies.

        Uses a *persistent* user-data-dir (``_SESSION_DIR``) so LinkedIn treats
        this as a returning browser with browsing history — significantly harder
        to fingerprint than a brand-new ephemeral context.

        If the login form is not found (LinkedIn showing a checkpoint/CAPTCHA),
        the stale session dir is cleared and one retry is performed with a fresh
        browser profile.
        """
        logger.info("[LinkedIn] Warming session via Playwright (this takes ~15 s) …")
        self._ensure_linkedin_state()
        creds = {
            "username": self._linkedin_username,
            "password": self._linkedin_password,
        }
        if creds.get("username"):
            logger.info(
                "[LinkedIn] Using "
                f"{self._linkedin_credential_source} credentials for "
                f"{mask_linkedin_username(creds['username'])}"
            )

        cookies: dict = {}
        for attempt in range(2):
            if attempt == 1:
                logger.warning(
                    "[LinkedIn] Login form was not visible — clearing stale scraper session "
                    "dir and retrying with fresh browser profile …"
                )
                _clear_session_dir(self._session_dir)

            login_form_found = True  # assume present; set False if wait_for_selector fails
            try:
                async with BrowserManager(headless=True, user_data_dir=self._session_dir) as bm:
                    page = await bm.new_page()

                    if creds.get("username") and creds.get("password"):
                        logger.info("[LinkedIn] Logging in to get authenticated session cookies …")
                        await page.goto(
                            "https://www.linkedin.com/login",
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        await bm.human_pause(1.5, 2.5)

                        current_url = page.url
                        if any(s in current_url for s in ("/feed", "/jobs", "/mynetwork")):
                            existing_raw = await page.context.cookies("https://www.linkedin.com")
                            existing_cookies = {
                                cookie["name"]: cookie["value"] for cookie in existing_raw
                            }
                            if self._has_authenticated_cookie(existing_cookies):
                                # Already authenticated via persistent profile
                                logger.info(f"[LinkedIn] Already authenticated (at {current_url})")
                                cookies = existing_cookies
                            else:
                                logger.warning(
                                    f"[LinkedIn] Reached {current_url} but no li_at cookie was present - "
                                    "treating session as stale and retrying with a fresh profile"
                                )
                                login_form_found = False
                        else:
                            # Expect the standard login form — wait with a short timeout so
                            # we detect checkpoint/CAPTCHA pages quickly instead of hanging
                            try:
                                await page.wait_for_selector(
                                    "input#username", state="visible", timeout=8_000
                                )
                                # Form is present — fill credentials
                                await page.fill("input#username", creds["username"])
                                await bm.human_pause(0.3, 0.8)
                                await page.fill("input#password", creds["password"])
                                await bm.human_pause(0.5, 1.0)
                                await page.click("button[type='submit']")
                                await bm.human_pause(3, 5)
                                for _ in range(4):
                                    raw_after_submit = await page.context.cookies("https://www.linkedin.com")
                                    cookies = {
                                        cookie["name"]: cookie["value"]
                                        for cookie in raw_after_submit
                                    }
                                    if self._has_authenticated_cookie(cookies):
                                        break
                                    await bm.human_pause(0.8, 1.4)
                                logger.info("[LinkedIn] Login submitted — waiting for redirect …")
                            except Exception:
                                logger.warning(
                                    f"[LinkedIn] Login form not visible at {current_url} "
                                    f"(attempt {attempt + 1}/2) — checkpoint/CAPTCHA suspected"
                                )
                                login_form_found = False

                    # Collect session cookies regardless of login outcome
                    await page.goto(
                        "https://www.linkedin.com/jobs/search/?keywords=software+engineer",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    await bm.human_pause(2, 4)
                    await page.evaluate("window.scrollBy(0, 600)")
                    await bm.human_pause(1, 2)
                    await page.evaluate("window.scrollBy(0, 400)")
                    await bm.human_pause(0.5, 1.5)
                    raw = await page.context.cookies("https://www.linkedin.com")
                    cookies = {c["name"]: c["value"] for c in raw}
                    has_auth = self._has_authenticated_cookie(cookies)
                    logger.info(
                        f"[LinkedIn] Session warm complete — {len(cookies)} cookies "
                        f"(authenticated={has_auth})"
                    )

            except Exception as exc:
                logger.warning(f"[LinkedIn] Session warming attempt {attempt + 1}/2 failed: {exc}")
                login_form_found = False  # treat crash as "form not found" to enable retry

            if self._has_authenticated_cookie(cookies):
                return cookies  # Authenticated — no need to retry

            if login_form_found or attempt == 1:
                # Either form was found (credentials wrong / checkpoint after submit),
                # or this was already the retry — don't loop again
                break
            # else: form was not found → outer loop will clear dir and retry

        if not self._has_authenticated_cookie(cookies):
            logger.warning(
                f"[LinkedIn] Could not obtain an authenticated session after {attempt + 1} attempt(s). "
                "Proceeding with unauthenticated cookies (scraping may be limited). "
                "To fix: set HEADLESS_BROWSER=false in .env, run once, and solve any "
                "LinkedIn security challenge manually — the session will persist."
            )
        return cookies

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:  # type: ignore[override]
        params = self._build_search_params(search_filter)
        seen_ids: set[str] = set()
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=search_filter.max_age_days)
            if search_filter.max_age_days
            else None
        )
        # Search-card Easy Apply badges are not consistently present in LinkedIn's
        # guest search HTML, so when easy_apply_only is enabled we defer the hard
        # filter until get_job_details() can confirm the job via Voyager/detail HTML.
        max_jobs = None if search_filter.easy_apply_only else search_filter.max_jobs
        total_yielded = 0

        for page in range(_MAX_PAGES):
            params["start"] = page * _PAGE_SIZE
            logger.debug(f"[LinkedIn] Fetching page {page + 1} (start={params['start']})")

            html = await self._fetch_search_page(params)

            if not html or html.strip() == "":
                if page == 0:
                    # First page empty — try Playwright direct scrape before giving up
                    logger.info(
                        "[LinkedIn] API returned empty on first page — trying Playwright fallback"
                    )
                    html = await self._search_playwright(params)
                if not html or html.strip() == "":
                    logger.info(f"[LinkedIn] No more results at page {page + 1}")
                    break

            cards = self._parse_job_cards(html)
            if not cards:
                break

            new_this_page = 0
            for card in cards:
                # Respect max_jobs limit
                if max_jobs is not None and total_yielded >= max_jobs:
                    logger.info(f"[LinkedIn] Reached max_jobs limit ({max_jobs}), stopping")
                    return

                job_id = card.get("job_id")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Age gate
                posted_at = card.get("posted_at")
                if cutoff and posted_at and posted_at < cutoff:
                    logger.debug(
                        f"[LinkedIn] Job {job_id} too old ({posted_at}), stopping pagination"
                    )
                    return

                # Skip excluded keywords
                title = card.get("title", "")
                if any(kw.lower() in title.lower() for kw in search_filter.exclude_keywords):
                    continue

                card_easy_apply = card.get("easy_apply", False)
                if search_filter.easy_apply_only and not card_easy_apply:
                    logger.debug(
                        f"[LinkedIn] Search card {job_id} has no Easy Apply badge - "
                        "verifying via detail fetch before filtering"
                    )

                job = self._make_job(
                    external_id=job_id,
                    title=title,
                    company=card.get("company", ""),
                    location=card.get("location"),
                    url=card.get("url", _JOB_VIEW_URL.format(job_id=job_id)),
                    posted_at=posted_at,
                    easy_apply=card_easy_apply,
                )
                new_this_page += 1
                total_yielded += 1
                yield job

            logger.info(f"[LinkedIn] Page {page + 1}: {new_this_page} new jobs")
            if new_this_page == 0:
                if search_filter.easy_apply_only:
                    logger.debug(
                        f"[LinkedIn] Page {page + 1} had no easy-apply matches - continuing pagination"
                    )
                    continue
                break

            # Randomized delay — breaks fixed-interval bot detection pattern
            await asyncio.sleep(max(1.0, settings.request_delay_seconds + random.uniform(-0.5, 2.0)))

    async def get_job_details(self, job: Job) -> Job:
        """Fetch full job details via a 3-tier cascade:

        1. LinkedIn Voyager JSON API  — fastest, requires authenticated li_at cookie.
        2. Guest posting HTML API     — server-rendered fragment, no JS needed.
        3. Playwright browser         — full JS rendering, guaranteed to work.

        Each tier falls through to the next if the description comes back empty.
        """
        if not job.external_id:
            return job

        # ── Tier 1: Voyager API (authenticated JSON) ──────────────────────────
        if self._cookies.get("li_at"):
            try:
                job = await self._fetch_details_voyager(job)
                if job.description:
                    return job
                logger.debug(f"[LinkedIn] Voyager returned no description for {job.external_id}")
            except Exception as exc:
                logger.debug(f"[LinkedIn] Voyager failed for {job.external_id}: {exc}")

        # ── Tier 2: Guest posting API (server-rendered HTML fragment) ─────────
        try:
            url = _JOB_POSTING_API_URL.format(job_id=job.external_id)
            html = await self._fetch_page(url)
            if html and len(html) > 200:
                job = self._parse_detail_html(html, job)
                if job.description:
                    return job
                logger.debug(f"[LinkedIn] Guest API HTML had no description for {job.external_id}")
        except Exception as exc:
            logger.debug(f"[LinkedIn] Guest API failed for {job.external_id}: {exc}")

        # ── Tier 3: Playwright browser (executes JS, uses auth session) ───────
        try:
            job = await self._fetch_details_playwright(job)
        except Exception as exc:
            logger.warning(f"[LinkedIn] All detail methods failed for {job.external_id}: {exc}")

        return job

    # ------------------------------------------------------------------
    # Detail fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_details_voyager(self, job: Job) -> Job:
        """Call LinkedIn's internal Voyager JSON API for job details.

        Uses the li_at auth cookie; JSESSIONID cookie value is the CSRF token.
        Response shape: {"data": {"description": {"text": "..."}, "employmentStatus": "..."}}
        """
        csrf = self._cookies.get("JSESSIONID", "").strip('"')
        url = _VOYAGER_JOB_URL.format(job_id=job.external_id)
        resp = await self._client.get(
            url,
            headers={
                "csrf-token": csrf,
                "X-Restli-Protocol-Version": "2.0.0",
                "X-Li-Lang": "en_US",
                "Accept": "application/vnd.linkedin.normalized+json+2.1",
                "X-Li-Track": (
                    '{"clientVersion":"1.13","osName":"web",'
                    '"timezoneOffset":-5,"timezone":"America/New_York",'
                    '"deviceFormFactor":"DESKTOP","mpName":"voyager-web"}'
                ),
            },
        )
        if resp.status_code != 200:
            raise ValueError(f"Voyager HTTP {resp.status_code}")

        data = resp.json()
        # Normalize: data may sit at root or under "data" key
        job_data = data.get("data", data)

        # Description
        desc_field = job_data.get("description") or {}
        text = desc_field.get("text", "") if isinstance(desc_field, dict) else ""
        if text.strip():
            job.description = text.strip()
            logger.debug(
                f"[LinkedIn] Voyager description ({len(job.description)} chars) for {job.external_id}"
            )

        # Employment type
        if not job.job_type:
            job.job_type = self._parse_job_type(
                job_data.get("employmentStatus", "")
            )

        # Remote/hybrid — prefer workRemoteAllowed bool, fall back to workplaceTypes list
        if not job.work_mode:
            if job_data.get("workRemoteAllowed") is True:
                job.work_mode = WorkMode.REMOTE
            else:
                for wt in (job_data.get("workplaceTypes") or []):
                    wt_lower = str(wt).lower()
                    if "remote" in wt_lower:
                        job.work_mode = WorkMode.REMOTE
                        break
                    if "hybrid" in wt_lower:
                        job.work_mode = WorkMode.HYBRID
                        break

        # Easy Apply detection via applyMethod.$type
        # ComplexOnsiteApply / SimpleOnsiteApply = LinkedIn hosts the form (Easy Apply)
        # OffsiteApply = redirect to external ATS (not Easy Apply)
        apply_method = job_data.get("applyMethod") or {}
        apply_type = apply_method.get("$type", "")
        if apply_type:
            job.easy_apply = "OnsiteApply" in apply_type
            logger.debug(
                f"[LinkedIn] Voyager applyMethod={apply_type!r} → easy_apply={job.easy_apply}"
            )

        return job

    async def _fetch_details_playwright(self, job: Job) -> Job:
        """Navigate to the job page in an authenticated Playwright browser.

        Uses a separate persistent session dir (_DETAIL_SESSION_DIR) with the
        current session cookies injected — avoids conflicting with _SESSION_DIR
        which is used by _warm_session().  The browser stays open across multiple
        calls (lazy-init) to avoid per-job browser startup overhead.
        """
        if self._detail_bm is None:
            self._detail_bm = BrowserManager(
                headless=True, user_data_dir=self._detail_session_dir
            )
            await self._detail_bm.start()
            await self._inject_detail_cookies()

        url = _JOB_VIEW_URL.format(job_id=job.external_id)
        page = await self._detail_bm.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Wait for React to render the description
            try:
                await page.wait_for_selector(
                    ".jobs-description__content, "
                    ".show-more-less-html__markup, "
                    "div[class*='jobs-description']",
                    timeout=8_000,
                )
            except Exception:
                pass  # proceed with whatever has rendered
            await BrowserManager.human_pause(1.0, 2.0)
            html = await page.content()
        finally:
            await page.close()

        if html and len(html) > 200:
            job = self._parse_detail_html(html, job)
            if job.description:
                logger.debug(
                    f"[LinkedIn] Playwright description ({len(job.description)} chars) for {job.external_id}"
                )
            else:
                logger.warning(
                    f"[LinkedIn] Playwright rendered page but no description for {job.external_id}"
                )
        return job

    async def _inject_detail_cookies(self) -> None:
        """Inject current session cookies into the detail Playwright context."""
        if not self._detail_bm or not self._detail_bm._context or not self._cookies:
            return
        cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".linkedin.com",
                "path": "/",
                "sameSite": "None",
                "secure": True,
            }
            for name, value in self._cookies.items()
        ]
        try:
            await self._detail_bm._context.add_cookies(cookies)
            logger.debug(f"[LinkedIn] Injected {len(cookies)} cookies into detail browser")
        except Exception as exc:
            logger.debug(f"[LinkedIn] Cookie injection error: {exc}")

    def _parse_detail_html(self, html: str, job: Job) -> Job:
        """Parse a job detail HTML page or fragment and enrich job fields.

        Tries JSON-LD structured data first, then falls back to CSS selectors.
        Used by both the guest API (HTML fragment) and Playwright (full page) tiers.
        """
        soup = BeautifulSoup(html, "lxml")

        # ── JSON-LD structured data (most stable — LinkedIn publishes for SEO) ─
        if not job.description:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    if isinstance(data, dict) and data.get("description"):
                        raw = data["description"]
                        if "<" in raw:
                            raw = BeautifulSoup(raw, "lxml").get_text(
                                separator="\n", strip=True
                            )
                        job.description = raw.strip()
                        if not job.job_type and data.get("employmentType"):
                            job.job_type = self._parse_job_type(data["employmentType"])
                        logger.debug(
                            f"[LinkedIn] JSON-LD description ({len(job.description)} chars) for {job.external_id}"
                        )
                        break
                except Exception:
                    pass

        # ── CSS selectors (fragment from guest API has these reliably) ─────────
        if not job.description:
            for sel in (
                "div.show-more-less-html__markup",
                "div.description__text",
                "section.show-more-less-html",
                "div.jobs-description__content",
                "div[class*='description__text']",
                "div[class*='jobs-description']",
            ):
                el = soup.select_one(sel)
                if el and el.get_text(strip=True):
                    job.description = el.get_text(separator="\n", strip=True)
                    logger.debug(
                        f"[LinkedIn] CSS description ({len(job.description)} chars) for {job.external_id}"
                    )
                    break

        if not job.description:
            logger.warning(f"[LinkedIn] No description extracted for {job.external_id}")
            logger.debug(f"[LinkedIn] HTML snippet: {html[:600]!r}")

        # ── Easy Apply ─────────────────────────────────────────────────────────
        # Try multiple selectors — LinkedIn UI varies between desktop/mobile/guest HTML
        apply_btn = (
            soup.select_one("button.jobs-apply-button[aria-label*='Easy Apply']")
            or soup.select_one("button[aria-label*='Easy Apply']")
            or soup.select_one("button.jobs-apply-button")
            or soup.select_one("a.jobs-apply-button")
        )
        if apply_btn:
            btn_text  = apply_btn.get_text(strip=True).lower()
            btn_label = (apply_btn.get("aria-label") or "").lower()
            job.easy_apply = "easy apply" in btn_text or "easy apply" in btn_label

        # ── Salary ─────────────────────────────────────────────────────────────
        salary_el = soup.select_one(
            "div.compensation__salary, span.salary.compensation__salary-range"
        )
        if salary_el:
            job.salary_min, job.salary_max, job.salary_currency = self._parse_salary(
                salary_el.get_text(strip=True)
            )

        # ── Job criteria chips ─────────────────────────────────────────────────
        for item in soup.select("li.description__job-criteria-item, .job-criteria__item"):
            header_el = item.select_one(
                "h3.description__job-criteria-subheader, .job-criteria__subheader"
            )
            value_el = item.select_one(
                "span.description__job-criteria-text, .job-criteria__text"
            )
            if not header_el or not value_el:
                continue
            header = header_el.get_text(strip=True).lower()
            value = value_el.get_text(strip=True).lower()
            if "employment type" in header:
                job.job_type = self._parse_job_type(value)
            elif "seniority level" in header:
                job.experience_level = self._parse_experience_level(value)

        # ── Skills ─────────────────────────────────────────────────────────────
        skill_els = soup.select(
            "a.job-details-skill-match-status-list__skill, .skill-pill"
        )
        if skill_els:
            job.skills = [s.get_text(strip=True) for s in skill_els if s.get_text(strip=True)]

        return job

    # ------------------------------------------------------------------
    # Session re-warm helper
    # ------------------------------------------------------------------

    async def _rewarm(self) -> None:
        """Close detail browser → warm session (uses _SESSION_DIR) → refresh clients.

        The detail browser and warm-session browser both use Playwright with
        persistent profiles.  Running them simultaneously on the same directory
        causes corruption, so we close the detail browser first.
        """
        # Close detail browser — it uses _DETAIL_SESSION_DIR, warm session uses _SESSION_DIR.
        # They're separate dirs so no conflict, but closing here also refreshes cookies.
        if self._detail_bm:
            try:
                await self._detail_bm.stop()
            except Exception:
                pass
            self._detail_bm = None

        self._cookies = await self._warm_session()
        if self._cookies:
            self._save_cookies(self._cookies)
        await self._client.close()
        self._client = self._make_client()
        if self.has_authenticated_session():
            self._detail_api_authwall_backoff_until = 0.0
        # Detail browser will be lazily re-created with fresh cookies on next use

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(_RateLimitError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=10, max=60),
        reraise=True,
    )
    async def _fetch_search_page(self, params: dict) -> str:
        url = f"{_GUEST_SEARCH_URL}?{urlencode(params)}"
        return await self._get(url)

    @retry(
        retry=retry_if_exception_type(_RateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),
        reraise=True,
    )
    async def _fetch_page(self, url: str) -> str:
        return await self._get(url)

    async def _get(self, url: str, _rewarm_attempted: bool = False) -> str:
        """GET *url*.  Rotates UA per request, re-warms on block/CAPTCHA."""
        # Rotate User-Agent on every request to avoid per-UA tracking
        self._client.headers.update({"User-Agent": self._ua.random})
        is_guest_detail_url = self._is_guest_job_posting_url(url)

        if (
            is_guest_detail_url
            and self._detail_api_authwall_backoff_until > time.time()
        ):
            remaining = int(self._detail_api_authwall_backoff_until - time.time())
            logger.debug(
                "[LinkedIn] Guest detail API backoff active "
                f"({remaining}s remaining) - falling back to browser verification"
            )
            return ""

        try:
            resp = await self._client.get(url)
        except Exception as exc:
            err_str = str(exc).lower()
            if "proxy" in err_str or "connect" in err_str:
                proxy = self._next_proxy()
                if proxy:
                    logger.warning("[LinkedIn] Proxy/connection error — rotating to next proxy")
                    await self._client.close()
                    self._client = self._make_client(proxy)
                raise _RateLimitError("Proxy/connection error")
            raise

        if resp.status_code == 429:
            logger.warning("[LinkedIn] Rate limited (429) — will back off and retry")
            raise _RateLimitError("429 Too Many Requests")

        if resp.status_code in (403, 999):
            # LinkedIn uses 999 for aggressive bot detection
            if not _rewarm_attempted:
                logger.warning(
                    f"[LinkedIn] Blocked ({resp.status_code}) - re-warming session"
                )
                await self._rewarm()
                return await self._get(url, _rewarm_attempted=True)
            if is_guest_detail_url:
                self._detail_api_authwall_backoff_until = (
                    time.time() + _DETAIL_API_AUTHWALL_BACKOFF_SECONDS
                )
                logger.warning(
                    "[LinkedIn] Guest detail API remained blocked after re-warm "
                    f"({resp.status_code}, authenticated={self.has_authenticated_session()}) - "
                    "backing off detail API and falling back to browser verification"
                )
                return ""
            logger.warning("[LinkedIn] Still blocked after re-warm - rotating proxy/UA")
            proxy = self._next_proxy()
            await self._client.close()
            self._client = self._make_client(proxy)
            raise _RateLimitError(f"Blocked with {resp.status_code}")

        resp.raise_for_status()
        html = resp.text

        # Detect auth-wall / CAPTCHA redirect in body
        if any(signal in html for signal in _CAPTCHA_SIGNALS):
            if not _rewarm_attempted:
                logger.warning("[LinkedIn] CAPTCHA/auth-wall detected - re-warming session")
                await self._rewarm()
                return await self._get(url, _rewarm_attempted=True)
            if is_guest_detail_url:
                self._detail_api_authwall_backoff_until = (
                    time.time() + _DETAIL_API_AUTHWALL_BACKOFF_SECONDS
                )
                logger.warning(
                    "[LinkedIn] Guest detail API still returned auth-wall after re-warm "
                    f"(authenticated={self.has_authenticated_session()}) - "
                    "backing off detail API and falling back to browser verification"
                )
                return ""
            logger.warning(
                "[LinkedIn] Still getting auth-wall after re-warm. "
                "Consider adding LinkedIn credentials or proxies (USE_PROXIES=true)."
            )
            return ""

        return html

    # ------------------------------------------------------------------
    # Playwright fallback
    # ------------------------------------------------------------------

    async def _search_playwright(self, params: dict) -> str:
        """
        Scrape the LinkedIn jobs search page directly with a stealth Playwright
        browser.  Used as a last resort when the guest API returns empty.

        The persistent session dir is reused so the browser already has cookies
        from ``_warm_session()``, making this effectively a cookie-authenticated
        browser scrape.
        """
        # Build browser-facing URL (uses different param names from the API)
        search_url = (
            "https://www.linkedin.com/jobs/search/?"
            + urlencode(
                {
                    "keywords": params.get("keywords", ""),
                    "location": params.get("location", ""),
                    "f_WT": params.get("f_WT", ""),
                    "f_JT": params.get("f_JT", ""),
                    "f_E": params.get("f_E", ""),
                    "f_TPR": params.get("f_TPR", ""),
                    "start": params.get("start", 0),
                }
            )
        )
        logger.info(f"[LinkedIn] Playwright fallback — loading: {search_url}")
        html = ""
        try:
            async with BrowserManager(headless=True, user_data_dir=self._session_dir) as bm:
                page = await bm.new_page()
                await page.goto(search_url, wait_until="networkidle", timeout=30_000)
                await bm.human_pause(2, 3)
                # Scroll to trigger lazy-loaded job cards
                for _ in range(4):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await bm.human_pause(0.8, 1.5)
                html = await page.content()
                logger.info("[LinkedIn] Playwright fallback — page captured")
        except Exception as exc:
            logger.warning(f"[LinkedIn] Playwright fallback failed: {exc}")
        return html

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_job_cards(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        cards = []
        for li in soup.select("li"):
            card_el = li.select_one("div.base-card, div.job-search-card")
            if not card_el:
                continue

            # Job ID from data-entity-urn="urn:li:jobPosting:12345"
            urn = card_el.get("data-entity-urn", "")
            job_id = urn.split(":")[-1] if urn else None

            if not job_id:
                # Try extracting from the link href
                link_el = card_el.select_one("a.base-card__full-link")
                if link_el:
                    href = link_el.get("href", "")
                    m = re.search(r"/jobs/view/(\d+)/", href)
                    if m:
                        job_id = m.group(1)

            if not job_id:
                continue

            # Title
            title_el = card_el.select_one(
                "h3.base-search-card__title, h3.job-search-card__title"
            )
            title = title_el.get_text(strip=True) if title_el else ""

            # Company
            company_el = card_el.select_one(
                "h4.base-search-card__subtitle a, a.hidden-nested-link"
            )
            if not company_el:
                company_el = card_el.select_one("h4.base-search-card__subtitle")
            company = company_el.get_text(strip=True) if company_el else ""

            # Location
            loc_el = card_el.select_one(
                "span.job-search-card__location, span.base-search-card__metadata--location"
            )
            location = loc_el.get_text(strip=True) if loc_el else None

            # URL — normalize locale subdomains (de.linkedin.com → www.linkedin.com)
            # to avoid ERR_TOO_MANY_REDIRECTS when Playwright navigates with .com cookies
            link_el = card_el.select_one("a.base-card__full-link")
            if link_el and link_el.get("href"):
                raw_url = link_el["href"].split("?")[0]
                url = re.sub(r"https://[a-z]{2}\.linkedin\.com/", "https://www.linkedin.com/", raw_url)
            else:
                url = _JOB_VIEW_URL.format(job_id=job_id)

            # Posted at
            time_el = card_el.select_one("time")
            posted_at: datetime | None = None
            if time_el and time_el.get("datetime"):
                try:
                    posted_at = datetime.fromisoformat(time_el["datetime"]).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

            # Work mode hint from location text
            work_mode: str | None = None
            if location:
                loc_lower = location.lower()
                if "remote" in loc_lower:
                    work_mode = WorkMode.REMOTE
                elif "hybrid" in loc_lower:
                    work_mode = WorkMode.HYBRID

            # Easy Apply badge in search result card
            # LinkedIn's guest HTML is inconsistent, so this is only an initial hint.
            easy_apply = False
            if card_el.select_one("li-icon[type='easy-apply-icon']"):
                easy_apply = True
            else:
                for el in card_el.select(
                    "span.result-benefits__text, "
                    "span.job-search-card__footer-item, "
                    "div.base-search-card__metadata span, "
                    "span[class*='benefit'], "
                    "span[class*='footer']"
                ):
                    text = el.get_text(" ", strip=True).lower()
                    label = (el.get("aria-label") or el.get("title") or "").lower()
                    if "easy apply" in text or "easy apply" in label:
                        easy_apply = True
                        break

            if not easy_apply:
                card_text = card_el.get_text(" ", strip=True).lower()
                easy_apply = "easy apply" in card_text

            cards.append(
                {
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "posted_at": posted_at,
                    "work_mode": work_mode,
                    "easy_apply": easy_apply,
                }
            )
        return cards

    # ------------------------------------------------------------------
    # Parameter builders
    # ------------------------------------------------------------------

    def _build_search_params(self, f: JobSearchFilter) -> dict:
        params: dict = {
            "keywords": " ".join(f.keywords),
            "count": _PAGE_SIZE,
        }

        if f.location:
            params["location"] = f.location

        # ── Work mode (f_WT) ──────────────────────────────────────────────────
        # Prefer the explicit work_modes list; fall back to remote_only flag.
        work_modes: list[str] = list(getattr(f, "work_modes", []))
        if f.remote_only and "remote" not in work_modes:
            work_modes.append("remote")

        if work_modes:
            wt_map = {"remote": "2", "hybrid": "3", "onsite": "1"}
            wt_codes = [wt_map[wm] for wm in work_modes if wm in wt_map]
            if wt_codes:
                params["f_WT"] = ",".join(dict.fromkeys(wt_codes))  # dedup, preserve order

        # ── Job type (f_JT) ───────────────────────────────────────────────────
        if f.job_types:
            jt_codes = [_JOB_TYPE_MAP[jt] for jt in f.job_types if jt in _JOB_TYPE_MAP]
            if jt_codes:
                params["f_JT"] = ",".join(jt_codes)

        # ── Experience level (f_E) ────────────────────────────────────────────
        if f.experience_levels:
            el_codes = [
                _EXP_LEVEL_MAP[el] for el in f.experience_levels if el in _EXP_LEVEL_MAP
            ]
            if el_codes:
                params["f_E"] = ",".join(dict.fromkeys(el_codes))

        # ── Date posted (f_TPR) ───────────────────────────────────────────────
        if f.max_age_days:
            params["f_TPR"] = f"r{f.max_age_days * 86400}"

        # ── Easy Apply only (f_LF) ────────────────────────────────────────────
        if getattr(f, "easy_apply_only", False):
            params["f_LF"] = "f_AL"

        return params

    # ------------------------------------------------------------------
    # Value parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_salary(text: str) -> tuple[float | None, float | None, str]:
        """Parse salary range from text like '$120K/yr – $160K/yr'."""
        currency = "USD"
        if "£" in text:
            currency = "GBP"
        elif "€" in text:
            currency = "EUR"

        text = text.replace(",", "").upper()
        numbers: list[float] = []
        for match in re.finditer(r"[\$£€]?\s*(\d+(?:\.\d+)?)\s*([KMk]?)", text):
            val = float(match.group(1))
            suffix = match.group(2).upper()
            if suffix == "K":
                val *= 1_000
            elif suffix == "M":
                val *= 1_000_000
            numbers.append(val)

        if len(numbers) >= 2:
            return numbers[0], numbers[1], currency
        elif len(numbers) == 1:
            return numbers[0], None, currency
        return None, None, currency

    @staticmethod
    def _parse_job_type(text: str) -> str | None:
        text = text.lower()
        if "full" in text:
            return JobType.FULL_TIME
        if "part" in text:
            return JobType.PART_TIME
        if "contract" in text:
            return JobType.CONTRACT
        if "intern" in text:
            return JobType.INTERNSHIP
        if "temp" in text:
            return JobType.TEMPORARY
        return None

    @staticmethod
    def _parse_experience_level(text: str) -> str | None:
        text = text.lower()
        if "intern" in text or "entry" in text or "junior" in text:
            return ExperienceLevel.ENTRY
        if "associate" in text or "mid" in text:
            return ExperienceLevel.MID
        if "senior" in text or "sr." in text:
            return ExperienceLevel.SENIOR
        if "lead" in text or "staff" in text or "principal" in text:
            return ExperienceLevel.LEAD
        if "director" in text or "vp" in text or "executive" in text:
            return ExperienceLevel.EXECUTIVE
        return None
