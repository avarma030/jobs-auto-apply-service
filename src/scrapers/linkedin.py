"""LinkedIn job scraper.

Scraping strategy
-----------------
LinkedIn exposes an unauthenticated guest-search endpoint that returns HTML
job-card fragments:

  GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
      ?keywords=<query>&location=<loc>&start=<offset>&count=25&...

Each page returns up to 25 <li> cards.  We paginate by bumping ``start`` in
steps of 25 until we get an empty response or hit ``max_pages``.

For full job descriptions we fetch each individual job page at
  https://www.linkedin.com/jobs/view/<job_id>/
and parse the description + detect the Easy Apply button.

No login is required for scraping.  Credentials in ``self.credentials`` are
used only by the applier (LinkedInApplier in src/appliers/linkedin.py).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.models import ExperienceLevel, Job, JobSearchFilter, JobType, WorkMode
from src.scrapers.base import BaseScraper

# ── Constants ──────────────────────────────────────────────────────────────────

_GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
_JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

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
_REQUEST_DELAY = 2.0  # seconds between requests


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
        ua = UserAgent()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": ua.random,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.linkedin.com/jobs/search/",
            },
            timeout=30,
            follow_redirects=True,
        )

    async def teardown(self) -> None:
        await self._client.aclose()
        await super().teardown()

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

        for page in range(_MAX_PAGES):
            params["start"] = page * _PAGE_SIZE
            logger.debug(f"[LinkedIn] Fetching page {page + 1} (start={params['start']})")

            html = await self._fetch_search_page(params)
            if not html or html.strip() == "":
                logger.info(f"[LinkedIn] No more results at page {page + 1}")
                break

            cards = self._parse_job_cards(html)
            if not cards:
                break

            new_this_page = 0
            for card in cards:
                job_id = card.get("job_id")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Age gate
                posted_at = card.get("posted_at")
                if cutoff and posted_at and posted_at < cutoff:
                    logger.debug(f"[LinkedIn] Job {job_id} too old ({posted_at}), stopping pagination")
                    return

                # Skip excluded keywords
                title = card.get("title", "")
                if any(kw.lower() in title.lower() for kw in search_filter.exclude_keywords):
                    continue

                job = self._make_job(
                    external_id=job_id,
                    title=title,
                    company=card.get("company", ""),
                    location=card.get("location"),
                    url=card.get("url", _JOB_VIEW_URL.format(job_id=job_id)),
                    posted_at=posted_at,
                )
                new_this_page += 1
                yield job

            logger.info(f"[LinkedIn] Page {page + 1}: {new_this_page} new jobs")
            if new_this_page == 0:
                break

            await asyncio.sleep(_REQUEST_DELAY)

    async def get_job_details(self, job: Job) -> Job:
        """Fetch the full job detail page and enrich *job* in-place."""
        if not job.external_id:
            return job

        url = _JOB_VIEW_URL.format(job_id=job.external_id)
        try:
            html = await self._fetch_page(url)
        except Exception as exc:
            logger.warning(f"[LinkedIn] Could not fetch details for {job.external_id}: {exc}")
            return job

        soup = BeautifulSoup(html, "lxml")

        # Description
        desc_el = soup.select_one(
            "div.show-more-less-html__markup, div.description__text"
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)

        # Easy Apply detection
        apply_btn = soup.select_one("button.jobs-apply-button")
        if apply_btn:
            btn_text = apply_btn.get_text(strip=True).lower()
            job.easy_apply = "easy apply" in btn_text

        # Salary
        salary_el = soup.select_one(
            "div.compensation__salary, span.salary.compensation__salary-range"
        )
        if salary_el:
            job.salary_min, job.salary_max, job.salary_currency = (
                self._parse_salary(salary_el.get_text(strip=True))
            )

        # Job type / work mode chips
        criteria_items = soup.select(
            "li.description__job-criteria-item, .job-criteria__item"
        )
        for item in criteria_items:
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
            elif "job function" in header:
                pass  # could populate tags
            elif "industries" in header:
                pass

        # Skills
        skill_els = soup.select("a.job-details-skill-match-status-list__skill, .skill-pill")
        if skill_els:
            job.skills = [s.get_text(strip=True) for s in skill_els if s.get_text(strip=True)]

        return job

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_search_page(self, params: dict) -> str:
        url = f"{_GUEST_SEARCH_URL}?{urlencode(params)}"
        resp = await self._client.get(url)
        if resp.status_code == 429:
            logger.warning("[LinkedIn] Rate limited (429) — backing off")
            await asyncio.sleep(30)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_page(self, url: str) -> str:
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.text

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

            # URL
            link_el = card_el.select_one("a.base-card__full-link")
            url = link_el["href"].split("?")[0] if link_el and link_el.get("href") else (
                _JOB_VIEW_URL.format(job_id=job_id)
            )

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

            cards.append(
                {
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "posted_at": posted_at,
                    "work_mode": work_mode,
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

        if f.remote_only:
            params["f_WT"] = "2"
        elif f.job_types:
            work_codes = [
                _WORK_TYPE_MAP[wt]
                for wt in [WorkMode.REMOTE, WorkMode.HYBRID, WorkMode.ONSITE]
                if wt in (f.job_types or [])
            ]
            if work_codes:
                params["f_WT"] = ",".join(work_codes)

        if f.job_types:
            jt_codes = [_JOB_TYPE_MAP[jt] for jt in f.job_types if jt in _JOB_TYPE_MAP]
            if jt_codes:
                params["f_JT"] = ",".join(jt_codes)

        if f.experience_levels:
            el_codes = [
                _EXP_LEVEL_MAP[el] for el in f.experience_levels if el in _EXP_LEVEL_MAP
            ]
            if el_codes:
                params["f_E"] = ",".join(set(el_codes))

        if f.max_age_days:
            params["f_TPR"] = f"r{f.max_age_days * 86400}"

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
