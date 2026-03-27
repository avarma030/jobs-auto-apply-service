from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Protocol
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.models import ExperienceLevel, Job, JobSearchFilter, JobType, WorkMode
from src.scrapers.base import BaseScraper
from src.utils.time import utcnow_naive


class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


class LinkedInScraper(BaseScraper):
    """Scrapes job listings from LinkedIn.

    Strategy:
    - Uses the LinkedIn Jobs search API (unauthenticated guest endpoint) for
      initial listing pages.
    - Falls back to Playwright-driven browser automation for full details and
      Easy Apply jobs when credentials are provided.
    """

    board_name = "LinkedIn"
    board_slug = "linkedin"
    requires_auth = False  # guest scraping supported; auth unlocks Easy Apply

    BASE_URL = "https://www.linkedin.com"
    JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
    JOBS_API_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    PAGE_SIZE = 25

    JOB_TYPE_PARAM_MAP = {
        JobType.FULL_TIME: "F",
        JobType.PART_TIME: "P",
        JobType.CONTRACT: "C",
        JobType.INTERNSHIP: "I",
        JobType.TEMPORARY: "T",
        JobType.FREELANCE: "V",
    }
    EXPERIENCE_PARAM_MAP = {
        ExperienceLevel.ENTRY: "2",
        ExperienceLevel.MID: "3",
        ExperienceLevel.SENIOR: "4",
        ExperienceLevel.LEAD: "5",
        ExperienceLevel.EXECUTIVE: "6",
    }
    JOB_TYPE_TEXT_MAP = {
        "full-time": JobType.FULL_TIME,
        "part-time": JobType.PART_TIME,
        "contract": JobType.CONTRACT,
        "internship": JobType.INTERNSHIP,
        "temporary": JobType.TEMPORARY,
        "freelance": JobType.FREELANCE,
    }
    EXPERIENCE_TEXT_MAP = {
        "internship": ExperienceLevel.ENTRY,
        "entry level": ExperienceLevel.ENTRY,
        "associate": ExperienceLevel.MID,
        "mid-senior level": ExperienceLevel.SENIOR,
        "director": ExperienceLevel.LEAD,
        "executive": ExperienceLevel.EXECUTIVE,
    }
    WORK_MODE_TEXT_MAP = {
        "remote": WorkMode.REMOTE,
        "hybrid": WorkMode.HYBRID,
        "on-site": WorkMode.ONSITE,
        "onsite": WorkMode.ONSITE,
    }
    RELATIVE_TIME_RE = re.compile(
        r"(?P<count>\d+)\s+(?P<unit>hour|day|week|month)s?\s+ago",
        re.IGNORECASE,
    )

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials=credentials)
        self._client: AsyncHttpClient | None = None

    async def setup(self) -> None:
        await super().setup()
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
                follow_redirects=True,
                timeout=20.0,
            )
        self._session_active = True

    async def teardown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._session_active = False
        await super().teardown()

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        """Yield jobs matching *search_filter* from LinkedIn."""
        logger.info(f"[LinkedIn] Starting search: {search_filter.keywords}")
        client = await self._ensure_client()
        seen: set[str] = set()
        start = 0

        while True:
            response = await client.get(
                self.JOBS_API_URL,
                params=self._build_search_params(search_filter, start=start),
            )
            response.raise_for_status()
            html = response.text.strip()
            if not html:
                break

            jobs = self._parse_search_results(html)
            if not jobs:
                break

            new_jobs = 0
            for job in jobs:
                dedupe_key = job.external_id or job.url
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                new_jobs += 1
                try:
                    yield await self.get_job_details(job)
                except Exception as exc:
                    self._log(
                        f"Failed to enrich job {job.external_id or job.url}: {exc}",
                        level="warning",
                    )
                    yield job

            if len(jobs) < self.PAGE_SIZE or new_jobs == 0:
                break
            start += self.PAGE_SIZE

    async def get_job_details(self, job: Job) -> Job:
        """Fetch the full job description page and enrich *job*."""
        client = await self._ensure_client()
        response = await client.get(job.url)
        response.raise_for_status()
        return self._enrich_job_details(job, response.text)

    async def _ensure_client(self) -> AsyncHttpClient:
        if self._client is None:
            await self.setup()
        assert self._client is not None
        return self._client

    def _build_search_params(self, search_filter: JobSearchFilter, *, start: int) -> dict[str, str | int]:
        keywords = " ".join(search_filter.keywords).strip()
        if search_filter.exclude_keywords:
            excluded = " ".join(f"-{keyword}" for keyword in search_filter.exclude_keywords)
            keywords = f"{keywords} {excluded}".strip()

        params: dict[str, str | int] = {"start": start}
        if keywords:
            params["keywords"] = keywords
        if search_filter.location:
            params["location"] = search_filter.location
        if search_filter.remote_only:
            params["f_WT"] = "2"

        job_types = [
            self.JOB_TYPE_PARAM_MAP[job_type]
            for job_type in search_filter.job_types
            if job_type in self.JOB_TYPE_PARAM_MAP
        ]
        if job_types:
            params["f_JT"] = ",".join(job_types)

        experience_levels = [
            self.EXPERIENCE_PARAM_MAP[level]
            for level in search_filter.experience_levels
            if level in self.EXPERIENCE_PARAM_MAP
        ]
        if experience_levels:
            params["f_E"] = ",".join(experience_levels)

        if search_filter.max_age_days > 0:
            params["f_TPR"] = f"r{search_filter.max_age_days * 24 * 60 * 60}"

        return params

    def _parse_search_results(self, html: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        cards = [card for card in soup.select(".base-search-card") if isinstance(card, Tag)]
        if not cards:
            cards = [card for card in soup.select("li") if isinstance(card, Tag)]

        jobs: list[Job] = []
        for card in cards:
            parsed = self._parse_job_card(card)
            if parsed is not None:
                jobs.append(parsed)
        return jobs

    def _parse_job_card(self, card: Tag) -> Job | None:
        nested_card = card.select_one(".base-search-card")
        if isinstance(nested_card, Tag):
            card = nested_card

        title = self._clean_text(
            card.select_one(".base-search-card__title, h3.base-search-card__title")
        )
        company = self._clean_text(
            card.select_one(".base-search-card__subtitle, h4.base-search-card__subtitle")
        )
        location = self._clean_text(card.select_one(".job-search-card__location"))
        link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")

        href = ""
        if isinstance(link_el, Tag):
            href = str(link_el.get("href", "")).strip()
        url = urljoin(self.BASE_URL, href) if href else ""

        if not title or not company or not url:
            return None

        return self._make_job(
            title=title,
            company=company,
            location=location or None,
            url=url,
            external_id=self._extract_external_id(card, url),
            posted_at=self._parse_posted_at(card),
            easy_apply="easy apply" in card.get_text(" ", strip=True).lower(),
        )

    def _enrich_job_details(self, job: Job, html: str) -> Job:
        soup = BeautifulSoup(html, "html.parser")
        description = self._clean_text(
            soup.select_one(".show-more-less-html__markup, .description__text, .jobs-description__content")
        )
        criteria = self._parse_job_criteria(soup)

        job_type = self._map_job_type(criteria.get("employment type")) or job.job_type
        experience_level = (
            self._map_experience_level(criteria.get("seniority level")) or job.experience_level
        )
        work_mode = (
            self._map_work_mode(criteria.get("workplace type"))
            or self._map_work_mode(job.location)
            or job.work_mode
        )
        tags = self._merge_tags(
            job.tags,
            criteria.get("job function"),
            criteria.get("industries"),
        )
        easy_apply = job.easy_apply or self._is_easy_apply_detail_page(soup)
        posted_at = job.posted_at or self._parse_posted_at(
            soup.select_one(".posted-time-ago__text, time") or soup
        )

        return job.model_copy(
            update={
                "description": description or job.description,
                "job_type": job_type,
                "experience_level": experience_level,
                "work_mode": work_mode,
                "tags": tags,
                "easy_apply": easy_apply,
                "posted_at": posted_at,
            }
        )

    def _parse_job_criteria(self, soup: BeautifulSoup) -> dict[str, str]:
        criteria: dict[str, str] = {}
        for item in soup.select(".description__job-criteria-item"):
            label = self._clean_text(item.select_one(".description__job-criteria-subheader")).lower()
            value = self._clean_text(item.select_one(".description__job-criteria-text"))
            if label and value:
                criteria[label] = value
        return criteria

    def _map_job_type(self, value: str | None) -> JobType | None:
        if not value:
            return None
        return self.JOB_TYPE_TEXT_MAP.get(value.strip().lower())

    def _map_experience_level(self, value: str | None) -> ExperienceLevel | None:
        if not value:
            return None
        return self.EXPERIENCE_TEXT_MAP.get(value.strip().lower())

    def _map_work_mode(self, value: str | None) -> WorkMode | None:
        if not value:
            return None
        normalized = value.strip().lower()
        for key, work_mode in self.WORK_MODE_TEXT_MAP.items():
            if key in normalized:
                return work_mode
        return None

    def _merge_tags(self, existing: list[str], *values: str | None) -> list[str]:
        tags = list(existing)
        seen = {tag.lower() for tag in tags}

        for value in values:
            if not value:
                continue
            for part in [segment.strip() for segment in value.split(",")]:
                if part and part.lower() not in seen:
                    tags.append(part)
                    seen.add(part.lower())
        return tags

    def _is_easy_apply_detail_page(self, soup: BeautifulSoup) -> bool:
        for cta in soup.select(".top-card-layout__cta"):
            if "easy apply" in self._clean_text(cta).lower():
                return True
        return False

    def _extract_external_id(self, card: Tag, url: str) -> str | None:
        for attr_name in ("data-entity-urn", "data-job-id"):
            attr_value = card.get(attr_name)
            if attr_value:
                match = re.search(r"(\d+)", str(attr_value))
                if match:
                    return match.group(1)

        path_match = re.search(r"/jobs/view/(\d+)", url)
        if path_match:
            return path_match.group(1)

        query_job_id = parse_qs(urlparse(url).query).get("currentJobId", [])
        return query_job_id[0] if query_job_id else None

    def _parse_posted_at(self, card: Tag, *, now: datetime | None = None) -> datetime | None:
        classes = set(card.get("class", []))
        if card.name == "time" or card.get("datetime") is not None or "posted-time-ago__text" in classes:
            time_el = card
        else:
            time_el = card.select_one("time")
        if not isinstance(time_el, Tag):
            return None

        posted_at = str(time_el.get("datetime", "")).strip()
        if posted_at:
            try:
                parsed = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed

        relative_label = self._clean_text(time_el)
        match = self.RELATIVE_TIME_RE.search(relative_label)
        if not match:
            return None

        count = int(match.group("count"))
        unit = match.group("unit").lower()
        if unit == "hour":
            delta = timedelta(hours=count)
        elif unit == "day":
            delta = timedelta(days=count)
        elif unit == "week":
            delta = timedelta(weeks=count)
        else:
            delta = timedelta(days=count * 30)

        return (now or utcnow_naive()) - delta

    @staticmethod
    def _clean_text(element: Tag | None) -> str:
        if not isinstance(element, Tag):
            return ""
        return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


class LinkedInApplier:
    """Handles Easy Apply and external apply flows on LinkedIn.

    Import from src.appliers.linkedin to keep concerns separated.
    Stub kept here for discoverability.
    """
