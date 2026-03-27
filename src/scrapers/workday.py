from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Protocol
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import settings
from src.models import Job, JobSearchFilter, JobType, WorkMode
from src.scrapers.base import BaseScraper
from src.utils.time import utcnow_naive


class AsyncHttpClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class WorkdayTenant:
    board_url: str
    jobs_api_url: str
    detail_api_root: str
    company_name: str


class WorkdayScraper(BaseScraper):
    """Scrapes jobs hosted on public Workday career portals."""

    board_name = "Workday"
    board_slug = "workday"
    requires_auth = False

    PAGE_SIZE = 20
    _POSTED_RE = re.compile(
        r"posted\s+(?:(?P<count>\d+)\s+(?P<unit>hour|day|week|month)s?\s+ago|(?P<special>today|yesterday))",
        re.IGNORECASE,
    )
    _JOB_TYPE_MAP: tuple[tuple[str, JobType], ...] = (
        ("full time", JobType.FULL_TIME),
        ("full-time", JobType.FULL_TIME),
        ("part time", JobType.PART_TIME),
        ("part-time", JobType.PART_TIME),
        ("contract", JobType.CONTRACT),
        ("intern", JobType.INTERNSHIP),
        ("temporary", JobType.TEMPORARY),
        ("freelance", JobType.FREELANCE),
        ("fixed term", JobType.TEMPORARY),
    )

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials=credentials)
        self._client: AsyncHttpClient | None = None
        self._detail_urls: dict[str, str] = {}

    async def setup(self) -> None:
        await super().setup()
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                },
                follow_redirects=True,
                timeout=20.0,
            )
        self._session_active = True

    async def teardown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._detail_urls.clear()
        self._session_active = False
        await super().teardown()

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        self._log(f"Starting search: {search_filter.keywords}")
        client = await self._ensure_client()
        tenants = self._tenant_configs()
        if not tenants:
            self._log("No Workday tenant URLs configured; skipping", level="warning")
            return

        seen: set[str] = set()
        search_text = " ".join(search_filter.keywords).strip()

        for tenant in tenants:
            offset = 0
            while True:
                response = await client.post(
                    tenant.jobs_api_url,
                    json={
                        "appliedFacets": {},
                        "limit": self.PAGE_SIZE,
                        "offset": offset,
                        "searchText": search_text,
                    },
                    headers={"Referer": tenant.board_url},
                )
                response.raise_for_status()
                payload = response.json()
                postings = payload.get("jobPostings") or []
                if not postings:
                    break

                new_jobs = 0
                for posting in postings:
                    job = self._job_from_posting(posting, tenant)
                    if not self._matches_listing_filter(job, search_filter):
                        continue

                    dedupe_key = job.url
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    new_jobs += 1

                    try:
                        job = await self.get_job_details(job)
                    except Exception as exc:
                        self._log(f"Failed to enrich job {job.url}: {exc}", level="warning")

                    if self._matches_final_filter(job, search_filter):
                        yield job

                if len(postings) < self.PAGE_SIZE or new_jobs == 0:
                    break
                offset += self.PAGE_SIZE

    async def get_job_details(self, job: Job) -> Job:
        client = await self._ensure_client()
        detail_url = self._detail_urls.get(job.url)
        if not detail_url:
            return job

        response = await client.get(detail_url, headers={"Referer": job.url})
        response.raise_for_status()
        payload = response.json()

        description_html = (
            payload.get("jobPostingInfo", {})
            .get("jobDescription")
            or payload.get("jobDescription")
            or ""
        )
        if description_html:
            job.description = BeautifulSoup(description_html, "html.parser").get_text("\n", strip=True)

        title = payload.get("title") or payload.get("jobPostingInfo", {}).get("title")
        if title:
            job.title = title

        location = payload.get("location") or payload.get("jobPostingInfo", {}).get("location")
        if not location:
            additional = payload.get("additionalLocations")
            if isinstance(additional, list) and additional:
                location = ", ".join(str(item) for item in additional if item)
        if location:
            job.location = location

        job_type_text = payload.get("timeType") or payload.get("workerSubType") or payload.get("jobType")
        job.job_type = self._parse_job_type(job_type_text)

        posted_label = payload.get("postedOn") or payload.get("jobPostingInfo", {}).get("postedOn")
        parsed_posted = self._parse_posted_at(posted_label)
        if parsed_posted is not None:
            job.posted_at = parsed_posted

        job.work_mode = self._infer_work_mode(job.location, description_html)

        return job

    async def _ensure_client(self) -> AsyncHttpClient:
        if self._client is None:
            await self.setup()
        assert self._client is not None
        return self._client

    def _tenant_configs(self) -> list[WorkdayTenant]:
        raw_urls: list[str] = []
        cred_urls = self.credentials.get("tenant_urls")
        if isinstance(cred_urls, str):
            raw_urls.extend(url.strip() for url in cred_urls.split(",") if url.strip())
        elif isinstance(cred_urls, list):
            raw_urls.extend(str(url).strip() for url in cred_urls if str(url).strip())
        raw_urls.extend(settings.workday_tenant_url_list())

        tenants: list[WorkdayTenant] = []
        seen: set[str] = set()
        for raw_url in raw_urls:
            tenant = self._parse_tenant_url(raw_url)
            if tenant.jobs_api_url in seen:
                continue
            seen.add(tenant.jobs_api_url)
            tenants.append(tenant)
        return tenants

    @classmethod
    def _parse_tenant_url(cls, raw_url: str) -> WorkdayTenant:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Unsupported Workday tenant URL: {raw_url}")

        path_parts = [part for part in parsed.path.split("/") if part]
        host = parsed.netloc.lower()

        if "myworkdayjobs.com" in host:
            company_slug = host.split(".")[0]
            if not path_parts:
                raise ValueError(f"Unsupported Workday tenant URL: {raw_url}")
            board_slug = path_parts[-1]
            board_path = "/" + "/".join(path_parts)
        elif "myworkdaysite.com" in host:
            if "recruiting" not in path_parts:
                raise ValueError(f"Unsupported Workday tenant URL: {raw_url}")
            recruiting_index = path_parts.index("recruiting")
            remaining = path_parts[recruiting_index + 1 :]
            if len(remaining) < 2:
                raise ValueError(f"Unsupported Workday tenant URL: {raw_url}")
            company_slug, board_slug = remaining[0], remaining[1]
            board_path = "/" + "/".join(path_parts[: recruiting_index + 3])
        else:
            raise ValueError(f"Unsupported Workday tenant URL: {raw_url}")

        api_root = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{company_slug}/{board_slug}"
        normalized_board_url = f"{parsed.scheme}://{parsed.netloc}{board_path}"
        return WorkdayTenant(
            board_url=normalized_board_url,
            jobs_api_url=f"{api_root}/jobs",
            detail_api_root=api_root,
            company_name=cls._humanize_company(company_slug),
        )

    def _job_from_posting(self, posting: dict[str, Any], tenant: WorkdayTenant) -> Job:
        external_path = str(posting.get("externalPath") or "").strip()
        browser_path = external_path if external_path.startswith("/") else f"/{external_path}"
        browser_url = f"{urlparse(tenant.board_url).scheme}://{urlparse(tenant.board_url).netloc}{browser_path}"
        detail_url = self._build_detail_url(tenant.detail_api_root, external_path)
        self._detail_urls[browser_url] = detail_url

        location = posting.get("locationsText") or posting.get("location")
        posted_at = self._parse_posted_at(posting.get("postedOn"))
        job = self._make_job(
            title=posting.get("title") or "Untitled role",
            company=tenant.company_name,
            location=location,
            description=None,
            url=browser_url,
            external_id=self._external_id_from_path(external_path),
            posted_at=posted_at,
            work_mode=self._infer_work_mode(location),
            tags=[field for field in posting.get("bulletFields", []) if isinstance(field, str)][:4],
            easy_apply=False,
        )
        return job

    @staticmethod
    def _build_detail_url(detail_api_root: str, external_path: str) -> str:
        cleaned = external_path.strip().lstrip("/")
        if cleaned.startswith("job/"):
            return f"{detail_api_root}/{cleaned}"
        return f"{detail_api_root}/job/{cleaned}"

    def _matches_listing_filter(self, job: Job, search_filter: JobSearchFilter) -> bool:
        if search_filter.exclude_keywords:
            haystack = " ".join(filter(None, [job.title, job.location or "", job.company])).lower()
            if any(keyword.lower() in haystack for keyword in search_filter.exclude_keywords):
                return False

        if search_filter.remote_only and job.work_mode != WorkMode.REMOTE:
            return False

        if search_filter.max_age_days > 0 and job.posted_at is not None:
            cutoff = utcnow_naive() - timedelta(days=search_filter.max_age_days)
            if job.posted_at < cutoff:
                return False

        return True

    def _matches_final_filter(self, job: Job, search_filter: JobSearchFilter) -> bool:
        if search_filter.job_types and job.job_type not in search_filter.job_types:
            return False
        if search_filter.experience_levels and job.experience_level not in search_filter.experience_levels:
            return False
        if search_filter.remote_only and job.work_mode != WorkMode.REMOTE:
            return False
        return True

    @classmethod
    def _parse_job_type(cls, value: Any) -> JobType | None:
        if not value:
            return None
        text = str(value).strip().lower()
        for token, job_type in cls._JOB_TYPE_MAP:
            if token in text:
                return job_type
        return None

    @classmethod
    def _parse_posted_at(cls, value: Any, *, now: datetime | None = None) -> datetime | None:
        if not value:
            return None
        label = str(value).strip()
        if not label:
            return None

        now = now or utcnow_naive()

        try:
            parsed = datetime.fromisoformat(label.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed

        match = cls._POSTED_RE.search(label)
        if not match:
            return None

        special = match.group("special")
        if special:
            if special.lower() == "today":
                return now
            return now - timedelta(days=1)

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
        return now - delta

    @staticmethod
    def _infer_work_mode(*values: Any) -> WorkMode | None:
        text = " ".join(str(value) for value in values if value).lower()
        if not text:
            return None
        if "hybrid" in text:
            return WorkMode.HYBRID
        if "remote" in text:
            return WorkMode.REMOTE
        if "on-site" in text or "on site" in text or "onsite" in text:
            return WorkMode.ONSITE
        return None

    @staticmethod
    def _external_id_from_path(path: str) -> str:
        trimmed = path.strip().rstrip("/")
        if not trimmed:
            return ""
        return trimmed.split("/")[-1]

    @staticmethod
    def _humanize_company(slug: str) -> str:
        return re.sub(r"[-_]+", " ", slug).title()
