from __future__ import annotations

import asyncio
import html
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from typing import Mapping
from urllib.parse import parse_qs

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.linkedin import LinkedInScraper

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

POSTED_WITHIN_TO_DAYS = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
}
POSTED_WITHIN_OPTIONS = [
    ("24h", "Last 24 hours"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
]


@dataclass(slots=True)
class DashboardQuery:
    keywords_raw: str = ""
    location: str = ""
    posted_within: str = "24h"
    remote_only: bool = False
    limit: int = 10

    @property
    def keywords(self) -> list[str]:
        return split_keywords(self.keywords_raw)

    @property
    def max_age_days(self) -> int:
        return POSTED_WITHIN_TO_DAYS.get(self.posted_within, 1)

    def to_search_filter(self) -> JobSearchFilter:
        return JobSearchFilter(
            keywords=self.keywords,
            location=self.location or None,
            remote_only=self.remote_only,
            max_age_days=self.max_age_days,
        )


def split_keywords(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if "," not in text and "\n" not in text:
        return [text]
    return [part.strip() for part in text.replace("\r", "").replace("\n", ",").split(",") if part.strip()]


def parse_dashboard_query(form_data: Mapping[str, list[str]]) -> DashboardQuery:
    posted_within = _first(form_data, "posted_within", "24h")
    if posted_within not in POSTED_WITHIN_TO_DAYS:
        posted_within = "24h"

    try:
        limit = int(_first(form_data, "limit", "10"))
    except ValueError:
        limit = 10

    return DashboardQuery(
        keywords_raw=_first(form_data, "keywords", ""),
        location=_first(form_data, "location", ""),
        posted_within=posted_within,
        remote_only=_first(form_data, "remote_only", "") == "on",
        limit=max(1, min(limit, 25)),
    )


async def run_dashboard_search(query: DashboardQuery) -> list[Job]:
    results: list[Job] = []
    async with LinkedInScraper() as scraper:
        async for job in scraper.search(query.to_search_filter()):
            results.append(job)
            if len(results) >= query.limit:
                break
    return results


def serve_dashboard(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    handler = _make_handler()
    with ThreadingHTTPServer((host, port), handler) as server:
        url_host = "localhost" if host == "0.0.0.0" else host
        dashboard_url = f"http://{url_host}:{port}"
        logger.info(f"Dashboard listening on {dashboard_url}")
        if open_browser:
            try:
                webbrowser.open(dashboard_url)
            except Exception as exc:
                logger.warning(f"Unable to open browser automatically: {exc}")
        server.serve_forever()


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "JobsAutoApplyDashboard/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", ""}:
                self._write_html(render_dashboard_page())
                return
            if self.path == "/health":
                self._write_text("ok")
                return
            if self.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/search":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            query = parse_dashboard_query(form_data)

            if not query.keywords:
                self._write_html(
                    render_dashboard_page(
                        query=query,
                        error="Enter at least one keyword before searching.",
                    ),
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            started = perf_counter()
            try:
                jobs = asyncio.run(run_dashboard_search(query))
            except Exception as exc:
                logger.exception("Dashboard search failed")
                self._write_html(
                    render_dashboard_page(
                        query=query,
                        error=f"Search failed: {exc}",
                    ),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            self._write_html(
                render_dashboard_page(
                    query=query,
                    results=jobs,
                    duration_seconds=perf_counter() - started,
                )
            )

        def log_message(self, format: str, *args: object) -> None:
            logger.debug(f"[dashboard] {format % args}")

        def _write_html(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _write_text(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return DashboardHandler


def render_dashboard_page(
    *,
    query: DashboardQuery | None = None,
    results: list[Job] | None = None,
    error: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    query = query or DashboardQuery(location="Ireland")
    results = results or []

    status_block = ""
    if error:
        status_block = f'<div class="banner banner--error">{escape_html(error)}</div>'
    elif results:
        duration_text = f" in {duration_seconds:.1f}s" if duration_seconds is not None else ""
        status_block = (
            f'<div class="banner banner--success">Found {len(results)} live LinkedIn job result(s){duration_text}.</div>'
        )

    result_markup = "".join(render_result_card(job) for job in results)
    if not result_markup:
        result_markup = """
        <div class="empty-state">
          <h2>No results yet</h2>
          <p>Run a search and the dashboard will render the real scraper output here.</p>
        </div>
        """

    options_markup = "".join(
        f'<option value="{value}"{" selected" if query.posted_within == value else ""}>{label}</option>'
        for value, label in POSTED_WITHIN_OPTIONS
    )

    remote_checked = " checked" if query.remote_only else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jobs Auto-Apply Dashboard</title>
  <style>
    :root {{
      --paper: #f6f0e6;
      --canvas: #f2efe8;
      --ink: #1f2a2a;
      --muted: #5e6b67;
      --panel: rgba(255, 252, 247, 0.88);
      --panel-strong: #fffdf8;
      --line: rgba(31, 42, 42, 0.12);
      --accent: #0f766e;
      --accent-warm: #b85c38;
      --accent-soft: #d7ebe8;
      --shadow: 0 24px 80px rgba(48, 56, 56, 0.12);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(184, 92, 56, 0.16), transparent 34%),
        radial-gradient(circle at top right, rgba(15, 118, 110, 0.16), transparent 28%),
        linear-gradient(180deg, #f8f3ea 0%, #ece7dd 100%);
      font-family: "Trebuchet MS", "Segoe UI", sans-serif;
    }}

    .shell {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }}

    .hero {{
      display: grid;
      grid-template-columns: 1.4fr 0.9fr;
      gap: 20px;
      margin-bottom: 20px;
    }}

    .hero-card,
    .panel {{
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--panel);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
    }}

    .hero-card {{
      padding: 28px;
    }}

    .hero h1 {{
      margin: 0 0 12px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }}

    .hero p {{
      margin: 0;
      max-width: 40rem;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }}

    .hero-note {{
      padding: 24px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 16px;
    }}

    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      padding: 8px 12px;
      font-size: 0.85rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 16px;
    }}

    .workspace {{
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 20px;
      align-items: start;
    }}

    .search-panel {{
      padding: 22px;
      position: sticky;
      top: 24px;
    }}

    .search-panel h2,
    .results-panel h2 {{
      margin: 0 0 14px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.5rem;
    }}

    label {{
      display: block;
      margin-bottom: 14px;
      font-size: 0.92rem;
      font-weight: 700;
      color: var(--ink);
    }}

    .hint {{
      display: block;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 0.8rem;
      font-weight: 500;
    }}

    input,
    select {{
      width: 100%;
      border: 1px solid rgba(31, 42, 42, 0.16);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.88);
      color: var(--ink);
      font: inherit;
    }}

    .checkbox-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 14px 0 18px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(15, 118, 110, 0.08);
      font-weight: 600;
    }}

    .checkbox-row input {{
      width: 18px;
      height: 18px;
      margin: 0;
    }}

    button {{
      width: 100%;
      border: 0;
      border-radius: 16px;
      padding: 14px 16px;
      background: linear-gradient(135deg, var(--accent) 0%, #125d58 100%);
      color: white;
      font: inherit;
      font-weight: 800;
      letter-spacing: 0.02em;
      cursor: pointer;
    }}

    button[disabled] {{
      opacity: 0.7;
      cursor: wait;
    }}

    .banner {{
      margin-bottom: 16px;
      padding: 14px 16px;
      border-radius: 16px;
      font-weight: 700;
    }}

    .banner--success {{
      background: rgba(15, 118, 110, 0.12);
      color: #125d58;
      border: 1px solid rgba(15, 118, 110, 0.2);
    }}

    .banner--error {{
      background: rgba(184, 92, 56, 0.12);
      color: #8f3f22;
      border: 1px solid rgba(184, 92, 56, 0.24);
    }}

    .results-panel {{
      padding: 22px;
    }}

    .results-grid {{
      display: grid;
      gap: 16px;
    }}

    .job-card {{
      border: 1px solid var(--line);
      border-radius: 22px;
      background: var(--panel-strong);
      padding: 20px;
      box-shadow: 0 14px 40px rgba(54, 61, 61, 0.08);
    }}

    .job-card h3 {{
      margin: 0 0 6px;
      font-size: 1.18rem;
      line-height: 1.3;
    }}

    .job-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }}

    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 14px 0;
    }}

    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 7px 10px;
      border-radius: 999px;
      background: #f2ece2;
      color: #4f5a57;
      font-size: 0.8rem;
      font-weight: 700;
    }}

    .card-footer {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 14px;
      margin-top: 18px;
    }}

    .apply-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 128px;
      padding: 11px 14px;
      border-radius: 14px;
      background: #1f2a2a;
      color: white;
      text-decoration: none;
      font-weight: 800;
    }}

    .subtle-note {{
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.55;
    }}

    .empty-state {{
      border: 1px dashed rgba(31, 42, 42, 0.22);
      border-radius: 22px;
      padding: 36px 24px;
      text-align: center;
      background: rgba(255, 255, 255, 0.5);
    }}

    .empty-state h2 {{
      margin: 0 0 8px;
      font-size: 1.3rem;
    }}

    @media (max-width: 900px) {{
      .hero,
      .workspace {{
        grid-template-columns: 1fr;
      }}

      .search-panel {{
        position: static;
      }}

      .card-footer {{
        flex-direction: column;
        align-items: stretch;
      }}

      .apply-link {{
        width: 100%;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">Live LinkedIn Search</div>
        <h1>Search real jobs without living in the terminal.</h1>
        <p>This dashboard calls the same LinkedIn scraper code as the CLI, enriches jobs from the detail pages, and renders the results as soon as you submit the form.</p>
      </div>
      <div class="hero-card hero-note">
        <div>
          <div class="eyebrow">Current Scope</div>
          <p>LinkedIn is live here today. The apply button currently opens the LinkedIn job page, which is the most reliable apply entry point exposed by the guest pages.</p>
        </div>
        <p class="subtle-note">Tip: keep searches small while testing locally to avoid LinkedIn rate limits.</p>
      </div>
    </section>

    <section class="workspace">
      <aside class="panel search-panel">
        <h2>Run Search</h2>
        <form method="post" action="/search" id="search-form">
          <label>
            <span class="hint">Keywords</span>
            <input type="text" name="keywords" value="{escape_html(query.keywords_raw)}" placeholder="project manager">
          </label>
          <label>
            <span class="hint">Location</span>
            <input type="text" name="location" value="{escape_html(query.location)}" placeholder="Ireland">
          </label>
          <label>
            <span class="hint">Posted within</span>
            <select name="posted_within">
              {options_markup}
            </select>
          </label>
          <label>
            <span class="hint">Max results</span>
            <input type="number" name="limit" min="1" max="25" value="{query.limit}">
          </label>
          <label class="checkbox-row">
            <input type="checkbox" name="remote_only"{remote_checked}>
            <span>Remote only</span>
          </label>
          <button type="submit" id="search-button">Run live scraper</button>
        </form>
      </aside>

      <section class="panel results-panel">
        <h2>Results</h2>
        {status_block}
        <div class="results-grid">
          {result_markup}
        </div>
      </section>
    </section>
  </main>
  <script>
    const form = document.getElementById("search-form");
    const button = document.getElementById("search-button");
    form?.addEventListener("submit", () => {{
      button.disabled = true;
      button.textContent = "Running live scraper...";
    }});
  </script>
</body>
</html>"""


def render_result_card(job: Job) -> str:
    meta_items = [
        escape_html(job.company),
        escape_html(job.location or "Location not listed"),
        escape_html(format_posted_at(job.posted_at)),
    ]
    if job.job_type:
        meta_items.append(escape_html(job.job_type.replace("_", " ").title()))
    if job.experience_level:
        meta_items.append(escape_html(job.experience_level.replace("_", " ").title()))
    if job.easy_apply:
        meta_items.append("Easy Apply")

    description = escape_html(shorten(job.description or "No description available yet.", limit=280))
    tags = "".join(f'<span class="pill">{escape_html(tag)}</span>' for tag in job.tags[:4])
    apply_href = escape_html(job.url)
    return f"""
    <article class="job-card">
      <h3>{escape_html(job.title)}</h3>
      <p>{escape_html(job.company)}</p>
      <div class="meta">{"".join(f'<span class="pill">{item}</span>' for item in meta_items)}</div>
      <p>{description}</p>
      <div class="meta">{tags}</div>
      <div class="card-footer">
        <p class="subtle-note">Apply link currently points to the LinkedIn job page surfaced by the scraper.</p>
        <a class="apply-link" href="{apply_href}" target="_blank" rel="noopener">Open Apply Link</a>
      </div>
    </article>
    """


def shorten(text: str, *, limit: int) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def format_posted_at(value: datetime | None) -> str:
    if value is None:
        return "Posted time unavailable"
    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M UTC")


def escape_html(value: str) -> str:
    return html.escape(value, quote=True)


def _first(data: Mapping[str, list[str]], key: str, default: str) -> str:
    values = data.get(key)
    if not values:
        return default
    return values[0]
