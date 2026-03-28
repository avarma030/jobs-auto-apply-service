from __future__ import annotations

import asyncio
import html
import json
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from typing import Mapping
from urllib.parse import parse_qs

from loguru import logger

from src.autopilot import AutopilotEngine
from src.config import settings
from src.models import (
    ApplicationRoute,
    AutopilotRun,
    AutomationState,
    JobAutomationResult,
    JobSearchFilter,
)
from src.scrapers.linkedin import LinkedInScraper
from src.scrapers.workday import WorkdayScraper
from src.utils import load_profile

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RECENT_SEARCHES_KEY = "jobs-auto-apply.recent-searches"
BOARD_OPTIONS = [
    ("linkedin", "LinkedIn"),
    ("workday", "Workday"),
]
SCRAPER_REGISTRY = {
    "linkedin": LinkedInScraper,
    "workday": WorkdayScraper,
}

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
QUICK_SEARCH_PRESETS = [
    {
        "label": "Project manager in Ireland",
        "keywords": "project manager",
        "location": "Ireland",
        "posted_within": "24h",
        "remote_only": False,
        "limit": 10,
    },
    {
        "label": "Remote Python engineer",
        "keywords": "python engineer",
        "location": "Remote",
        "posted_within": "24h",
        "remote_only": True,
        "limit": 12,
    },
    {
        "label": "Product manager in Dublin",
        "keywords": "product manager",
        "location": "Dublin",
        "posted_within": "7d",
        "remote_only": False,
        "limit": 12,
    },
    {
        "label": "Data analyst in London",
        "keywords": "data analyst",
        "location": "London",
        "posted_within": "24h",
        "remote_only": False,
        "limit": 10,
    },
]


@dataclass(slots=True)
class DashboardQuery:
    board: str = "linkedin"
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
    board = _first(form_data, "board", "linkedin")
    if board not in dict(BOARD_OPTIONS):
        board = "linkedin"

    posted_within = _first(form_data, "posted_within", "24h")
    if posted_within not in POSTED_WITHIN_TO_DAYS:
        posted_within = "24h"

    try:
        limit = int(_first(form_data, "limit", "10"))
    except ValueError:
        limit = 10

    return DashboardQuery(
        board=board,
        keywords_raw=_first(form_data, "keywords", ""),
        location=_first(form_data, "location", ""),
        posted_within=posted_within,
        remote_only=_first(form_data, "remote_only", "") == "on",
        limit=max(1, min(limit, 25)),
    )


async def run_dashboard_search(query: DashboardQuery) -> AutopilotRun:
    if query.board == "workday" and not settings.workday_tenant_url_list():
        raise ValueError("Set WORKDAY_TENANT_URLS before running Workday searches.")

    scraper_cls = SCRAPER_REGISTRY.get(query.board)
    if scraper_cls is None:
        raise ValueError(f"Unsupported board: {query.board}")

    profile = load_profile(settings.user_profile_path)
    live_apply_routes = (
        {ApplicationRoute.LINKEDIN_EASY_APPLY}
        if query.board == "linkedin"
        else set()
    )
    engine = AutopilotEngine(profile=profile, live_apply_routes=live_apply_routes)
    return await engine.run_search(
        board=query.board,
        scraper_cls=scraper_cls,
        search_filter=query.to_search_filter(),
        limit=query.limit,
        scan_cap=settings.linkedin_dashboard_scan_cap if query.board == "linkedin" else None,
    )


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
                run = asyncio.run(run_dashboard_search(query))
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
                    run=run,
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
    run: AutopilotRun | None = None,
    error: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    query = query or DashboardQuery(location="Ireland")
    run = run or AutopilotRun(board=query.board)
    results = run.results
    excluded_results = run.excluded_results
    has_results = bool(results)
    has_excluded_results = bool(excluded_results)
    has_run = run.total_scraped > 0 or has_results or has_excluded_results
    board_label = board_display_name(query.board)

    if error:
        status_banner = f"""
        <div class="status status--error">
          <div class="status__label">Search issue</div>
          <p>{escape_html(error)}</p>
        </div>
        """
    elif has_run:
        if query.board == "linkedin":
            status_copy = (
                f"Scraped {run.total_scraped} {escape_html(board_label)} job(s), "
                f"filtered out {run.filtered_out_count} low-fit job(s), "
                f"excluded {run.excluded_external_count} external and {run.excluded_unconfirmed_count} unconfirmed job(s), "
                f"kept {run.shortlisted_count} visible Easy Apply match(es), "
                f"submitted {run.auto_applied_count}, and held back {run.ats_blocked_count}{format_duration(duration_seconds)}."
            )
        else:
            status_copy = (
                f"Scraped {run.total_scraped} {escape_html(board_label)} job(s), "
                f"filtered out {run.filtered_out_count}, "
                f"kept {run.shortlisted_count} visible shortlisted match(es), "
                f"submitted {run.auto_applied_count}, and held back {run.ats_blocked_count}{format_duration(duration_seconds)}."
            )
        status_banner = f"""
        <div class="status status--success">
          <div class="status__label">Autopilot run complete</div>
          <p>{status_copy}</p>
        </div>
        """
    else:
        status_banner = """
        <div class="status status--neutral">
          <div class="status__label">Ready to search</div>
          <p>Use a preset or type your own search. One run will scrape, score, tailor, and route the jobs below.</p>
        </div>
        """

    result_sections: list[str] = []
    if has_results:
        active_copy = (
            "Only LinkedIn Easy Apply jobs stay in the live phase-one list."
            if query.board == "linkedin"
            else "Shortlisted matches stay visible here while the handler route is prepared."
        )
        visible_cards = "".join(
            render_result_card(result, index=index) for index, result in enumerate(results, start=1)
        )
        result_sections.append(
            f"""
            <section class="result-block">
              <div class="section-title">
                <div>
                  <h2>Active shortlist</h2>
                  <p>{escape_html(active_copy)}</p>
                </div>
              </div>
              <div class="results-grid">
                {visible_cards}
              </div>
            </section>
            """
        )
    elif query.board == "linkedin" and has_excluded_results:
        result_sections.append(render_linkedin_no_visible_state(run))
    else:
        result_sections.append(render_empty_state())

    if has_excluded_results:
        result_sections.append(render_excluded_results_section(excluded_results))

    result_markup = "".join(result_sections)

    options_markup = "".join(
        f'<option value="{value}"{" selected" if query.posted_within == value else ""}>{label}</option>'
        for value, label in POSTED_WITHIN_OPTIONS
    )
    board_options_markup = "".join(
        f'<option value="{value}"{" selected" if query.board == value else ""}>{label}</option>'
        for value, label in BOARD_OPTIONS
    )
    presets_markup = "".join(render_preset_chip(preset, index=index) for index, preset in enumerate(QUICK_SEARCH_PRESETS))
    remote_checked = " checked" if query.remote_only else ""
    body_class = "page page--results" if has_results else "page"
    preset_data_json = escape_html(json.dumps(QUICK_SEARCH_PRESETS))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jobs Auto-Apply Dashboard</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --ink: #112126;
      --muted: #5d6b70;
      --line: rgba(17, 33, 38, 0.12);
      --panel: rgba(255, 252, 247, 0.84);
      --panel-solid: #fffaf2;
      --panel-dark: #14252a;
      --teal: #0d7c72;
      --teal-deep: #0c615a;
      --terracotta: #bf6542;
      --gold: #c49a35;
      --success: #d9efe7;
      --danger: #f5dfd7;
      --shadow-soft: 0 22px 70px rgba(28, 39, 42, 0.1);
      --shadow-card: 0 12px 36px rgba(28, 39, 42, 0.08);
      --radius-xl: 30px;
      --radius-lg: 22px;
      --radius-md: 16px;
    }}

    * {{
      box-sizing: border-box;
    }}

    html {{
      scroll-behavior: smooth;
    }}

    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 12%, rgba(191, 101, 66, 0.18), transparent 24%),
        radial-gradient(circle at 88% 8%, rgba(13, 124, 114, 0.22), transparent 28%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.18), rgba(255, 255, 255, 0)),
        linear-gradient(180deg, #f7f1e8 0%, #ebe2d3 100%);
      font-family: "Trebuchet MS", Verdana, sans-serif;
      min-height: 100vh;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(17, 33, 38, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17, 33, 38, 0.03) 1px, transparent 1px);
      background-size: 36px 36px;
      mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.6), transparent 90%);
    }}

    .shell {{
      width: min(1260px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 56px;
      position: relative;
      z-index: 1;
    }}

    .masthead {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(310px, 0.85fr);
      gap: 20px;
      margin-bottom: 22px;
    }}

    .panel {{
      position: relative;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      background: var(--panel);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-soft);
    }}

    .panel::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      border-radius: inherit;
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.28), rgba(255, 255, 255, 0));
    }}

    .hero {{
      padding: 28px;
      min-height: 260px;
      background:
        radial-gradient(circle at top right, rgba(13, 124, 114, 0.16), transparent 32%),
        radial-gradient(circle at bottom left, rgba(196, 154, 53, 0.18), transparent 26%);
    }}

    .hero__eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(13, 124, 114, 0.12);
      color: var(--teal-deep);
      font-size: 0.82rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }}

    .hero h1 {{
      margin: 18px 0 14px;
      max-width: 11ch;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(2.4rem, 5vw, 4.6rem);
      line-height: 0.92;
      letter-spacing: -0.05em;
    }}

    .hero p {{
      margin: 0;
      max-width: 44rem;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.65;
    }}

    .hero__meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}

    .hero__meta-chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 999px;
      border: 1px solid rgba(17, 33, 38, 0.08);
      background: rgba(255, 255, 255, 0.7);
      color: var(--ink);
      font-size: 0.86rem;
      font-weight: 700;
    }}

    .insight-stack {{
      display: grid;
      gap: 16px;
      padding: 18px;
    }}

    .insight-card {{
      padding: 18px;
      border-radius: 24px;
      background: var(--panel-solid);
      border: 1px solid rgba(17, 33, 38, 0.08);
      box-shadow: var(--shadow-card);
    }}

    .insight-card h2 {{
      margin: 0 0 8px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.2rem;
    }}

    .insight-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }}

    .insight-list {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}

    .insight-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(13, 124, 114, 0.08);
      font-size: 0.88rem;
    }}

    .workspace {{
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 20px;
      align-items: start;
    }}

    .search-panel {{
      padding: 20px;
      position: sticky;
      top: 20px;
    }}

    .search-section + .search-section {{
      margin-top: 18px;
    }}

    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 12px;
    }}

    .section-title h2,
    .results-head h2 {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.45rem;
    }}

    .section-title p,
    .results-head p {{
      margin: 0;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.5;
    }}

    .preset-grid,
    .recent-grid,
    .query-strip,
    .results-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}

    .preset-chip,
    .recent-chip,
    .summary-chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.75);
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
      text-align: left;
      transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
    }}

    .preset-chip small {{
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 700;
    }}

    .preset-chip:hover,
    .recent-chip:hover,
    .summary-chip:hover {{
      transform: translateY(-1px);
      border-color: rgba(13, 124, 114, 0.28);
      background: rgba(13, 124, 114, 0.08);
    }}

    .recent-block {{
      display: none;
    }}

    .recent-block.is-visible {{
      display: block;
    }}

    .launch-card,
    .shortcut-card {{
      padding: 16px;
      border-radius: 20px;
      background:
        radial-gradient(circle at top right, rgba(13, 124, 114, 0.1), transparent 30%),
        rgba(255, 255, 255, 0.78);
      border: 1px solid rgba(17, 33, 38, 0.08);
      box-shadow: var(--shadow-card);
    }}

    .launch-card {{
      display: grid;
      gap: 12px;
    }}

    .launch-card h3,
    .shortcut-card h3 {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.18rem;
    }}

    .launch-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: fit-content;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(13, 124, 114, 0.1);
      color: var(--teal-deep);
      font-size: 0.76rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .launch-meta {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.9rem;
    }}

    .shortcut-grid {{
      display: grid;
      gap: 10px;
    }}

    .shortcut-card p {{
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 0.88rem;
    }}

    .shortcut-key {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 38px;
      padding: 7px 10px;
      border-radius: 12px;
      background: rgba(17, 33, 38, 0.08);
      color: var(--ink);
      font-size: 0.82rem;
      font-weight: 800;
    }}

    .form-grid {{
      display: grid;
      gap: 14px;
    }}

    .field {{
      display: grid;
      gap: 6px;
    }}

    .field label {{
      color: var(--ink);
      font-size: 0.82rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .field-hint {{
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.45;
    }}

    input,
    select {{
      width: 100%;
      padding: 14px 16px;
      border: 1px solid rgba(17, 33, 38, 0.14);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.82);
      color: var(--ink);
      font: inherit;
      transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }}

    input:focus,
    select:focus {{
      outline: none;
      border-color: rgba(13, 124, 114, 0.42);
      box-shadow: 0 0 0 5px rgba(13, 124, 114, 0.08);
      background: #fff;
    }}

    .inline-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}

    .toggle-card {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(13, 124, 114, 0.08);
      border: 1px solid rgba(13, 124, 114, 0.12);
    }}

    .toggle-copy {{
      display: grid;
      gap: 3px;
    }}

    .toggle-copy strong {{
      font-size: 0.95rem;
    }}

    .toggle-copy span {{
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.4;
    }}

    .toggle-card input {{
      width: 20px;
      height: 20px;
      margin: 0;
    }}

    .actions {{
      display: flex;
      gap: 10px;
    }}

    .button,
    .link-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 50px;
      padding: 0 18px;
      border-radius: 16px;
      border: 0;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      transition: transform 120ms ease, box-shadow 120ms ease, opacity 120ms ease;
      text-decoration: none;
    }}

    .button:hover,
    .link-button:hover {{
      transform: translateY(-1px);
    }}

    .button[disabled] {{
      opacity: 0.75;
      cursor: wait;
      transform: none;
    }}

    .button--primary {{
      flex: 1;
      color: #fff;
      background: linear-gradient(135deg, var(--teal) 0%, var(--teal-deep) 100%);
      box-shadow: 0 16px 26px rgba(13, 124, 114, 0.24);
    }}

    .button--ghost,
    .link-button--secondary {{
      background: rgba(17, 33, 38, 0.06);
      color: var(--ink);
      border: 1px solid rgba(17, 33, 38, 0.1);
    }}

    .link-button--primary {{
      background: linear-gradient(135deg, var(--teal) 0%, var(--teal-deep) 100%);
      color: #fff;
      box-shadow: 0 16px 26px rgba(13, 124, 114, 0.2);
    }}

    .results-panel {{
      padding: 20px;
    }}

    .results-head {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: end;
      margin-bottom: 16px;
    }}

    .status {{
      margin-bottom: 16px;
      padding: 16px 18px;
      border-radius: 18px;
      border: 1px solid transparent;
    }}

    .status__label {{
      margin-bottom: 6px;
      font-size: 0.8rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }}

    .status p {{
      margin: 0;
      line-height: 1.55;
    }}

    .status--success {{
      background: var(--success);
      border-color: rgba(13, 124, 114, 0.16);
      color: var(--teal-deep);
    }}

    .status--error {{
      background: var(--danger);
      border-color: rgba(191, 101, 66, 0.24);
      color: #934a2f;
    }}

    .status--neutral {{
      background: rgba(255, 255, 255, 0.72);
      border-color: rgba(17, 33, 38, 0.08);
      color: var(--ink);
    }}

    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}

    .stat-card {{
      padding: 16px;
      border-radius: 18px;
      background: var(--panel-solid);
      border: 1px solid rgba(17, 33, 38, 0.08);
      box-shadow: var(--shadow-card);
    }}

    .stat-card__label {{
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 800;
    }}

    .stat-card__value {{
      margin-top: 8px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.9rem;
      line-height: 1;
    }}

    .summary-chip {{
      cursor: default;
      background: rgba(255, 255, 255, 0.66);
    }}

    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 9px 12px;
      border-radius: 999px;
      background: rgba(17, 33, 38, 0.06);
      color: #405055;
      font-size: 0.82rem;
      font-weight: 800;
    }}

    .pill--signal {{
      background: rgba(13, 124, 114, 0.1);
      color: var(--teal-deep);
    }}

    .pill--warm {{
      background: rgba(196, 154, 53, 0.14);
      color: #8e6b1f;
    }}

    .result-card {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 16px;
      padding: 20px;
      border-radius: 26px;
      background:
        radial-gradient(circle at top right, rgba(13, 124, 114, 0.08), transparent 28%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(255, 250, 242, 0.82));
      border: 1px solid rgba(17, 33, 38, 0.08);
      box-shadow: var(--shadow-card);
      transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }}

    .result-card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 18px 42px rgba(28, 39, 42, 0.12);
      border-color: rgba(13, 124, 114, 0.18);
    }}

    .result-card--featured {{
      border-color: rgba(13, 124, 114, 0.22);
      background:
        radial-gradient(circle at top right, rgba(13, 124, 114, 0.14), transparent 28%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(246, 255, 251, 0.88));
    }}

    .company-badge {{
      width: 68px;
      height: 68px;
      border-radius: 22px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.25rem;
      font-weight: 800;
      box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.25);
    }}

    .result-card__content {{
      display: grid;
      gap: 14px;
      min-width: 0;
    }}

    .result-card__top {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
    }}

    .result-card__title-wrap {{
      min-width: 0;
    }}

    .result-card__flag {{
      display: inline-flex;
      align-items: center;
      margin-bottom: 8px;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(13, 124, 114, 0.1);
      color: var(--teal-deep);
      font-size: 0.78rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .result-card__title {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.45rem;
      line-height: 1.05;
    }}

    .result-card__company {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 0.95rem;
      font-weight: 700;
    }}

    .result-card__rank {{
      flex: none;
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(17, 33, 38, 0.06);
      color: var(--ink);
      font-size: 0.86rem;
      font-weight: 800;
    }}

    .result-card__meta,
    .result-card__tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    .result-card__description {{
      margin: 0;
      color: var(--muted);
      line-height: 1.68;
      font-size: 0.95rem;
    }}

    .result-card__footer {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: end;
    }}

    .result-card__note {{
      margin: 0;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.55;
      max-width: 34rem;
    }}

    .result-card__actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}

    .results-grid {{
      display: grid;
      gap: 16px;
    }}

    .results-stack {{
      display: grid;
      gap: 18px;
    }}

    .result-block {{
      display: grid;
      gap: 14px;
    }}

    .result-block--muted {{
      padding-top: 4px;
      border-top: 1px solid rgba(17, 33, 38, 0.08);
    }}

    .result-card--excluded {{
      border-style: dashed;
      background:
        linear-gradient(180deg, rgba(255, 252, 247, 0.98), rgba(248, 242, 232, 0.92));
    }}

    .empty-state {{
      display: grid;
      gap: 16px;
      padding: 28px;
      border-radius: 26px;
      background:
        radial-gradient(circle at top right, rgba(191, 101, 66, 0.12), transparent 24%),
        linear-gradient(180deg, rgba(255, 252, 247, 0.94), rgba(255, 250, 242, 0.84));
      border: 1px dashed rgba(17, 33, 38, 0.18);
      box-shadow: var(--shadow-card);
    }}

    .empty-state--diagnostic {{
      background:
        radial-gradient(circle at top right, rgba(196, 154, 53, 0.18), transparent 24%),
        linear-gradient(180deg, rgba(255, 252, 247, 0.96), rgba(251, 246, 238, 0.9));
    }}

    .empty-state h3 {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.5rem;
    }}

    .empty-state p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
      max-width: 42rem;
    }}

    .empty-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}

    .empty-card {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(17, 33, 38, 0.08);
    }}

    .empty-card strong {{
      display: block;
      margin-bottom: 6px;
    }}

    .empty-card span {{
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.5;
    }}

    .toast {{
      position: fixed;
      right: 20px;
      bottom: 20px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(20, 37, 42, 0.94);
      color: white;
      font-size: 0.9rem;
      font-weight: 700;
      box-shadow: var(--shadow-soft);
      opacity: 0;
      pointer-events: none;
      transform: translateY(12px);
      transition: opacity 160ms ease, transform 160ms ease;
    }}

    .toast.is-visible {{
      opacity: 1;
      transform: translateY(0);
    }}

    @media (max-width: 1060px) {{
      .workspace {{
        grid-template-columns: 1fr;
      }}

      .search-panel {{
        position: static;
      }}

      .stats-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}

    @media (max-width: 860px) {{
      .masthead {{
        grid-template-columns: 1fr;
      }}

      .results-head,
      .result-card__top,
      .result-card__footer {{
        flex-direction: column;
        align-items: start;
      }}

      .empty-grid {{
        grid-template-columns: 1fr;
      }}
    }}

    @media (max-width: 640px) {{
      .shell {{
        width: min(100vw - 20px, 1260px);
        padding-top: 18px;
      }}

      .hero,
      .search-panel,
      .results-panel,
      .result-card {{
        padding: 18px;
      }}

      .inline-grid,
      .stats-grid {{
        grid-template-columns: 1fr;
      }}

      .actions,
      .result-card__actions {{
        flex-direction: column;
      }}

      .result-card {{
        grid-template-columns: 1fr;
      }}

      .company-badge,
      .link-button {{
        width: 100%;
      }}
    }}
  </style>
</head>
<body class="{body_class}">
  <main class="shell">
    <section class="masthead">
      <section class="panel hero">
        <div class="hero__eyebrow">Live {escape_html(board_label)} autopilot console</div>
        <h1>Search once. Let the application system take it from there.</h1>
        <p>This dashboard runs the live scraper, scores the jobs against the profile, tailors the application package, and routes each match into the right apply path without making the user babysit the process.</p>
        <div class="hero__meta">
          <span class="hero__meta-chip">{escape_html(board_label)} selected</span>
          <span class="hero__meta-chip">Compatibility scoring</span>
          <span class="hero__meta-chip">ATS tailoring</span>
          <span class="hero__meta-chip">Handler routing</span>
        </div>
      </section>
      <aside class="panel insight-stack">
        <section class="insight-card">
          <h2>Built for one input</h2>
          <p>Search criteria are the only user action. The run behind it handles scraping, filtering, tailoring, and handler selection before a job ever shows up in the list.</p>
        </section>
        <section class="insight-card">
          <h2>Autopilot priorities</h2>
          <div class="insight-list">
            <div class="insight-row"><span>Only show jobs above the match threshold</span><strong>75%+</strong></div>
            <div class="insight-row"><span>Tailor the application package before apply</span><strong>90% ATS</strong></div>
            <div class="insight-row"><span>Route each job into the right handler</span><strong>Automatic</strong></div>
          </div>
        </section>
      </aside>
    </section>

    <section class="workspace">
      <aside class="panel search-panel">
        <section class="search-section">
          <div class="section-title">
            <div>
              <h2>Search setup</h2>
              <p>Start with a preset or define the exact jobs the autopilot should chase.</p>
            </div>
          </div>
          <div class="preset-grid">{presets_markup}</div>
        </section>

        <section class="search-section recent-block" id="recent-block">
          <div class="section-title">
            <div>
              <h2>Recent searches</h2>
              <p>Saved in your browser for quick reruns.</p>
            </div>
          </div>
          <div class="recent-grid" id="recent-grid"></div>
        </section>

        {render_action_panel(results)}

        <section class="search-section">
          <form method="post" action="/search" id="search-form">
            <div class="form-grid">
              <div class="field">
                <label for="board">Source board</label>
                <select id="board" name="board">
                  {board_options_markup}
                </select>
                <div class="field-hint">Choose the board to scrape. The scoring, tailoring, and routing flow stays the same.</div>
              </div>

              <div class="field">
                <label for="keywords">Keywords</label>
                <input id="keywords" type="text" name="keywords" value="{escape_html(query.keywords_raw)}" placeholder="project manager">
                <div class="field-hint">Use one phrase or separate multiple ideas with commas.</div>
              </div>

              <div class="field">
                <label for="location">Location</label>
                <input id="location" type="text" name="location" value="{escape_html(query.location)}" placeholder="Ireland">
              </div>

              <div class="inline-grid">
                <div class="field">
                  <label for="posted_within">Posted within</label>
                  <select id="posted_within" name="posted_within">
                    {options_markup}
                  </select>
                </div>
                <div class="field">
                  <label for="limit">Max results</label>
                  <input id="limit" type="number" name="limit" min="1" max="25" value="{query.limit}">
                </div>
              </div>

              <label class="toggle-card" for="remote_only">
                <span class="toggle-copy">
                  <strong>Remote only</strong>
                  <span>Keep the query focused on remote-friendly results.</span>
                </span>
                <input id="remote_only" type="checkbox" name="remote_only"{remote_checked}>
              </label>

              <div class="actions">
                <button type="submit" class="button button--primary" id="search-button">Run autopilot</button>
                <button type="button" class="button button--ghost" id="clear-button">Clear</button>
              </div>
            </div>
          </form>
        </section>
      </aside>

      <section class="panel results-panel" id="results-section">
        <div class="results-head">
          <div>
            <h2>Results</h2>
            <p>Each card comes from the real {escape_html(board_label)} scraper output, then gets scored, tailored, and routed into the right application path.</p>
          </div>
          <div class="actions">
            <button type="button" class="button button--ghost" id="copy-search-button">Copy search summary</button>
          </div>
        </div>

        {status_banner}
        {render_stat_cards(run, duration_seconds)}
        <div class="query-strip">{render_query_chips(query)}</div>
        <div class="results-stack">
          {result_markup}
        </div>
      </section>
    </section>
  </main>

  <div class="toast" id="toast">Copied to clipboard</div>

  <script type="application/json" id="preset-data">{preset_data_json}</script>
  <script>
    const form = document.getElementById("search-form");
    const button = document.getElementById("search-button");
    const clearButton = document.getElementById("clear-button");
    const copySearchButton = document.getElementById("copy-search-button");
    const toast = document.getElementById("toast");
    const recentGrid = document.getElementById("recent-grid");
    const recentBlock = document.getElementById("recent-block");
    const presetData = JSON.parse(document.getElementById("preset-data").textContent || "[]");
    const resultsSection = document.getElementById("results-section");
    const fields = {{
      board: document.getElementById("board"),
      keywords: document.getElementById("keywords"),
      location: document.getElementById("location"),
      postedWithin: document.getElementById("posted_within"),
      remoteOnly: document.getElementById("remote_only"),
      limit: document.getElementById("limit"),
    }};

    function isTypingTarget(target) {{
      if (!target) return false;
      const tagName = target.tagName;
      return tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT" || target.isContentEditable;
    }}

    function showToast(message) {{
      if (!toast) return;
      toast.textContent = message;
      toast.classList.add("is-visible");
      window.clearTimeout(showToast._timer);
      showToast._timer = window.setTimeout(() => toast.classList.remove("is-visible"), 1800);
    }}

    function currentSearchPayload() {{
      return {{
        board: fields.board?.value || "linkedin",
        keywords: fields.keywords?.value?.trim() || "",
        location: fields.location?.value?.trim() || "",
        posted_within: fields.postedWithin?.value || "24h",
        remote_only: !!fields.remoteOnly?.checked,
        limit: fields.limit?.value || "10",
      }};
    }}

    function applySearchPayload(payload) {{
      if (payload.board && fields.board) {{
        fields.board.value = payload.board;
      }}
      fields.keywords.value = payload.keywords || "";
      fields.location.value = payload.location || "";
      fields.postedWithin.value = payload.posted_within || "24h";
      fields.remoteOnly.checked = !!payload.remote_only;
      fields.limit.value = payload.limit || 10;
    }}

    function renderRecentSearches() {{
      if (!recentGrid || !recentBlock) return;
      const items = JSON.parse(localStorage.getItem("{RECENT_SEARCHES_KEY}") || "[]");
      recentGrid.innerHTML = "";
      if (!items.length) {{
        recentBlock.classList.remove("is-visible");
        return;
      }}

      recentBlock.classList.add("is-visible");
      items.forEach((item) => {{
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "recent-chip";
        btn.textContent = item.label;
        btn.addEventListener("click", () => {{
          applySearchPayload(item.payload);
          fields.keywords.focus();
        }});
        recentGrid.appendChild(btn);
      }});
    }}

    function saveRecentSearch(payload) {{
      if (!payload.keywords) return;
      const labelParts = [payload.board || "linkedin", payload.keywords];
      if (payload.location) labelParts.push(payload.location);
      labelParts.push(payload.posted_within);
      const entry = {{
        label: labelParts.join(" | "),
        payload,
      }};
      const existing = JSON.parse(localStorage.getItem("{RECENT_SEARCHES_KEY}") || "[]")
        .filter((item) => item.label !== entry.label);
      const next = [entry, ...existing].slice(0, 6);
      localStorage.setItem("{RECENT_SEARCHES_KEY}", JSON.stringify(next));
    }}

    document.querySelectorAll("[data-preset-index]").forEach((buttonEl) => {{
      buttonEl.addEventListener("click", () => {{
        const preset = presetData[Number(buttonEl.dataset.presetIndex)];
        if (!preset) return;
        applySearchPayload(preset);
        fields.keywords.focus();
      }});
    }});

    document.querySelectorAll("[data-copy-url]").forEach((buttonEl) => {{
      buttonEl.addEventListener("click", async () => {{
        try {{
          await navigator.clipboard.writeText(buttonEl.dataset.copyUrl);
          showToast("Job link copied");
        }} catch (error) {{
          showToast("Copy failed");
        }}
      }});
    }});

    copySearchButton?.addEventListener("click", async () => {{
      const payload = currentSearchPayload();
      const summary = [
        payload.board || "linkedin",
        payload.keywords || "No keywords",
        payload.location || "Any location",
        payload.posted_within,
        payload.remote_only ? "remote only" : "mixed location",
        "limit " + payload.limit,
      ].join(" | ");
      try {{
        await navigator.clipboard.writeText(summary);
        showToast("Search summary copied");
      }} catch (error) {{
        showToast("Copy failed");
      }}
    }});

    clearButton?.addEventListener("click", () => {{
      applySearchPayload({{
        board: fields.board?.value || "linkedin",
        keywords: "",
        location: "",
        posted_within: "24h",
        remote_only: false,
        limit: 10,
      }});
      fields.keywords.focus();
    }});

    document.addEventListener("keydown", (event) => {{
      if (event.key === "/" && !isTypingTarget(event.target)) {{
        event.preventDefault();
        fields.keywords.focus();
        fields.keywords.select();
      }}

      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {{
        if (!form || button?.disabled) return;
        event.preventDefault();
        form.requestSubmit();
      }}
    }});

    form?.addEventListener("submit", () => {{
      const payload = currentSearchPayload();
      saveRecentSearch(payload);
      button.disabled = true;
      button.textContent = "Running autopilot...";
    }});

    if ({str(has_run).lower()} || {str(bool(error)).lower()}) {{
      setTimeout(() => resultsSection?.scrollIntoView({{ behavior: "smooth", block: "start" }}), 120);
    }}

    renderRecentSearches();
  </script>
</body>
</html>"""


def render_stat_cards(run: AutopilotRun, duration_seconds: float | None) -> str:
    cards = [("Scraped", str(run.total_scraped))]
    if run.board == "linkedin":
        cards.extend(
            [
                ("Low fit", str(run.filtered_out_count)),
                ("Excluded ext", str(run.excluded_external_count)),
                ("Excluded unsure", str(run.excluded_unconfirmed_count)),
                ("Visible", str(run.shortlisted_count)),
                ("Applied", str(run.auto_applied_count)),
                ("Needs review", str(run.ats_blocked_count)),
                ("Runtime", format_runtime(duration_seconds)),
            ]
        )
    else:
        cards.extend(
            [
                ("Filtered out", str(run.filtered_out_count)),
                ("Visible", str(run.shortlisted_count)),
                ("Applied", str(run.auto_applied_count)),
                ("Needs review", str(run.ats_blocked_count)),
                ("Runtime", format_runtime(duration_seconds)),
            ]
        )
    markup = "".join(
        f"""
        <article class="stat-card">
          <div class="stat-card__label">{escape_html(label)}</div>
          <div class="stat-card__value">{escape_html(value)}</div>
        </article>
        """
        for label, value in cards
    )
    return f'<div class="stats-grid">{markup}</div>'


def render_query_chips(query: DashboardQuery) -> str:
    keywords = ", ".join(query.keywords) if query.keywords else "No keywords yet"
    posted_label = dict(POSTED_WITHIN_OPTIONS).get(query.posted_within, "Last 24 hours")
    chips = [
        f"Board: {board_display_name(query.board)}",
        f"Keywords: {shorten(keywords, limit=42)}",
        f"Location: {shorten(query.location or 'Any location', limit=30)}",
        f"Window: {posted_label}",
        "Mode: Remote only" if query.remote_only else "Mode: Mixed location",
        f"Limit: {query.limit}",
    ]
    return "".join(f'<span class="summary-chip">{escape_html(chip)}</span>' for chip in chips)


def render_preset_chip(preset: dict[str, object], *, index: int) -> str:
    detail = " | ".join(
        [
            str(preset.get("location") or "Any location"),
            dict(POSTED_WITHIN_OPTIONS).get(str(preset.get("posted_within") or "24h"), "Last 24 hours"),
            f'limit {preset.get("limit") or 10}',
        ]
    )
    return f"""
    <button type="button" class="preset-chip" data-preset-index="{index}">
      <span>{escape_html(str(preset.get("label") or "Preset"))}</span>
      <small>{escape_html(detail)}</small>
    </button>
    """


def render_empty_state() -> str:
    return """
    <article class="empty-state">
      <h3>Your autopilot shortlist will land here.</h3>
      <p>Pick a preset or enter your own search criteria to launch the scrape, scoring, and tailoring flow. When jobs arrive, this panel turns into a ranked application workspace with route and handler decisions already made.</p>
      <div class="empty-grid">
        <div class="empty-card">
          <strong>Start narrow</strong>
          <span>Use one title and one location first so the compatibility filter stays honest.</span>
        </div>
        <div class="empty-card">
          <strong>Lean on thresholds</strong>
          <span>Only jobs above the match threshold stay in the list, so empty states are useful signal too.</span>
        </div>
        <div class="empty-card">
          <strong>Inspect the handoff</strong>
          <span>Each result shows the handler, ATS score, and apply status instead of just dumping raw scrape output.</span>
        </div>
      </div>
    </article>
    """


def render_linkedin_no_visible_state(run: AutopilotRun) -> str:
    return f"""
    <article class="empty-state empty-state--diagnostic">
      <h3>No Easy Apply jobs cleared the active LinkedIn shortlist yet.</h3>
      <p>The search still found promising LinkedIn jobs, but {run.excluded_external_count} use external apply flows and {run.excluded_unconfirmed_count} could not confirm Easy Apply. They are listed below so you can inspect what was excluded instead of seeing a blank shortlist.</p>
      <div class="empty-grid">
        <div class="empty-card">
          <strong>Visible list stays strict</strong>
          <span>Phase one only keeps confirmed Easy Apply jobs in the active shortlist.</span>
        </div>
        <div class="empty-card">
          <strong>Excluded jobs stay inspectable</strong>
          <span>High-fit external or unconfirmed roles are still shown with their route and exclusion reason.</span>
        </div>
        <div class="empty-card">
          <strong>Search can scan deeper</strong>
          <span>The dashboard keeps scanning past excluded jobs until it hits the LinkedIn scan cap or finds enough visible Easy Apply matches.</span>
        </div>
      </div>
    </article>
    """


def render_action_panel(results: list[JobAutomationResult]) -> str:
    if not results:
        return """
        <section class="search-section">
          <div class="section-title">
            <div>
              <h2>Shortcuts</h2>
              <p>Built to feel fast even when the scraper takes a moment.</p>
            </div>
          </div>
          <div class="shortcut-grid">
            <article class="shortcut-card">
              <span class="shortcut-key">/</span>
              <h3>Jump to search</h3>
              <p>Press slash from anywhere on the page to focus the keyword box instantly.</p>
            </article>
            <article class="shortcut-card">
              <span class="shortcut-key">Ctrl + Enter</span>
              <h3>Run without reaching</h3>
              <p>Submit the current search from the keyboard when your filters are ready.</p>
            </article>
          </div>
        </section>
        """

    top_result = results[0]
    top_job = top_result.job
    summary = " | ".join(
        part for part in [top_job.company, top_job.location or "Location not listed", format_posted_at(top_job.posted_at)] if part
    )
    actions = "".join(
        [
            f'<button type="button" class="button button--ghost" data-copy-url="{escape_html(top_job.url)}">Copy top link</button>',
            f'<a class="link-button link-button--primary" href="{escape_html(top_job.url)}" target="_blank" rel="noopener">Open top match</a>',
        ]
    )
    return f"""
    <section class="search-section">
      <div class="section-title">
        <div>
          <h2>Next best move</h2>
          <p>Your first result is pinned here so you can act without scrolling.</p>
        </div>
      </div>
      <article class="launch-card">
        <span class="launch-label">Top result</span>
        <h3>{escape_html(top_job.title)}</h3>
        <p class="launch-meta">{escape_html(summary)}</p>
        <div class="result-card__tags">
          {render_pill(f"Match {top_result.compatibility_score}%", kind="signal")}
          {render_pill(f"ATS {top_result.ats_score}%", kind="warm")}
          {render_pill(route_display_name(top_result.route))}
          {render_pill(automation_state_label(top_result.automation_state), kind=automation_state_kind(top_result.automation_state))}
          {render_pill(str(top_job.job_type).replace("_", " ").title()) if top_job.job_type else ""}
          {render_pill(str(top_job.experience_level).replace("_", " ").title(), kind="warm") if top_job.experience_level else ""}
        </div>
        <p class="launch-meta">{escape_html(automation_note(top_result))}</p>
        <div class="actions">
          {actions}
        </div>
      </article>
    </section>
    """


def render_excluded_results_section(results: list[JobAutomationResult]) -> str:
    cards = "".join(
        render_excluded_result_card(result, index=index)
        for index, result in enumerate(results, start=1)
    )
    return f"""
    <section class="result-block result-block--muted">
      <div class="section-title">
        <div>
          <h2>Excluded from active LinkedIn list</h2>
          <p>These jobs scored well enough to inspect, but they are outside the current Easy Apply-only live flow.</p>
        </div>
      </div>
      <div class="results-grid">
        {cards}
      </div>
    </section>
    """


def render_result_card(result: JobAutomationResult, *, index: int) -> str:
    job = result.job
    meta_items = [
        render_pill(job.location or "Location not listed"),
        render_pill(format_posted_at(job.posted_at), kind="warm"),
        render_pill(f"Match {result.compatibility_score}%", kind="signal"),
        render_pill(f"ATS {result.ats_score}%", kind="warm"),
    ]
    if job.job_type:
        meta_items.append(render_pill(str(job.job_type).replace("_", " ").title()))
    if job.experience_level:
        meta_items.append(render_pill(str(job.experience_level).replace("_", " ").title()))
    if job.work_mode:
        meta_items.append(render_pill(str(job.work_mode).replace("_", " ").title()))
    if job.easy_apply:
        meta_items.append(render_pill("Easy Apply", kind="signal"))
    meta_items.append(render_pill(route_display_name(result.route)))
    meta_items.append(
        render_pill(
            automation_state_label(result.automation_state),
            kind=automation_state_kind(result.automation_state),
        )
    )

    signal_terms = [*result.matched_keywords[:3], *[f"Tailored: {term}" for term in result.added_keywords[:2]]]
    tags_markup = "".join(render_pill(tag, kind="signal") for tag in signal_terms) or render_pill("Profile alignment ready")
    description = escape_html(shorten(job.description or "No description available yet.", limit=320))
    apply_href = escape_html(job.url)
    featured_flag = '<div class="result-card__flag">Top result</div>' if index == 1 else ""
    card_class = "result-card result-card--featured" if index == 1 else "result-card"
    return f"""
    <article class="{card_class}">
      <div class="company-badge" style="background: {company_gradient(job.company)};">{escape_html(company_monogram(job.company))}</div>
      <div class="result-card__content">
        <div class="result-card__top">
          <div class="result-card__title-wrap">
            {featured_flag}
            <h3 class="result-card__title">{escape_html(job.title)}</h3>
            <p class="result-card__company">{escape_html(job.company)}</p>
          </div>
          <div class="result-card__rank">#{index:02d}</div>
        </div>

        <div class="result-card__meta">
          {''.join(meta_items)}
        </div>

        <p class="result-card__description">{description}</p>

        <div class="result-card__tags">
          {tags_markup}
        </div>

        <div class="result-card__footer">
          <p class="result-card__note">{escape_html(automation_note(result))}</p>
          <div class="result-card__actions">
            <button type="button" class="link-button link-button--secondary" data-copy-url="{apply_href}">Copy link</button>
            <a class="link-button link-button--primary" href="{apply_href}" target="_blank" rel="noopener">Open Apply Link</a>
          </div>
        </div>
      </div>
    </article>
    """


def render_excluded_result_card(result: JobAutomationResult, *, index: int) -> str:
    job = result.job
    meta_items = [
        render_pill(job.location or "Location not listed"),
        render_pill(format_posted_at(job.posted_at), kind="warm"),
        render_pill(f"Match {result.compatibility_score}%", kind="signal"),
        render_pill(route_display_name(result.route)),
        render_pill(exclusion_reason_label(result.exclusion_reason), kind="warm"),
    ]
    description = escape_html(shorten(job.description or "No description available yet.", limit=260))
    apply_href = escape_html(job.url)
    reason = exclusion_reason_help(result.exclusion_reason)
    signal_terms = "".join(
        render_pill(term, kind="signal") for term in result.compatibility_reasons[:3]
    ) or render_pill("High-fit result")
    return f"""
    <article class="result-card result-card--excluded">
      <div class="company-badge" style="background: {company_gradient(job.company)};">{escape_html(company_monogram(job.company))}</div>
      <div class="result-card__content">
        <div class="result-card__top">
          <div class="result-card__title-wrap">
            <div class="result-card__flag">Excluded #{index:02d}</div>
            <h3 class="result-card__title">{escape_html(job.title)}</h3>
            <p class="result-card__company">{escape_html(job.company)}</p>
          </div>
        </div>
        <div class="result-card__meta">
          {''.join(meta_items)}
        </div>
        <p class="result-card__description">{description}</p>
        <div class="result-card__tags">
          {signal_terms}
        </div>
        <div class="result-card__footer">
          <p class="result-card__note">{escape_html(reason)}</p>
          <div class="result-card__actions">
            <button type="button" class="link-button link-button--secondary" data-copy-url="{apply_href}">Copy link</button>
            <a class="link-button link-button--primary" href="{apply_href}" target="_blank" rel="noopener">Open job page</a>
          </div>
        </div>
      </div>
    </article>
    """


def render_pill(label: str, *, kind: str | None = None) -> str:
    class_name = "pill"
    if kind == "signal":
        class_name += " pill--signal"
    elif kind == "warm":
        class_name += " pill--warm"
    return f'<span class="{class_name}">{escape_html(label)}</span>'


def count_unique_companies(results: list[JobAutomationResult]) -> int:
    return len({result.job.company.strip().lower() for result in results if result.job.company.strip()})


def company_monogram(company: str) -> str:
    initials = [part[0] for part in company.split() if part and part[0].isalnum()]
    if not initials:
        return "J"
    return "".join(initials[:2]).upper()


def company_gradient(company: str) -> str:
    seed = sum(ord(char) for char in company.lower())
    hue_a = seed % 360
    hue_b = (hue_a + 42) % 360
    return f"linear-gradient(135deg, hsl({hue_a} 58% 48%), hsl({hue_b} 72% 36%))"


def board_display_name(board: str) -> str:
    return dict(BOARD_OPTIONS).get(board, board.title())


def route_display_name(route: object) -> str:
    route_value = getattr(route, "value", route)
    labels = {
        "linkedin_easy_apply": "LinkedIn Easy Apply",
        "linkedin_external": "External site",
        "greenhouse": "Greenhouse",
        "workday": "Workday",
        "lever": "Lever",
        "generic": "Generic handler",
    }
    return labels.get(str(route_value), str(route_value).replace("_", " ").title())


def exclusion_reason_label(reason: str | None) -> str:
    labels = {
        "linkedin_external": "External apply",
        "easy_apply_unconfirmed": "Easy Apply unconfirmed",
    }
    return labels.get(str(reason), "Excluded")


def exclusion_reason_help(reason: str | None) -> str:
    messages = {
        "linkedin_external": "This job matched the profile, but LinkedIn routed it to an external application page instead of Easy Apply.",
        "easy_apply_unconfirmed": "This job matched the profile, but the scraper could not confirm an Easy Apply flow after fetching the detail page.",
    }
    return messages.get(str(reason), "This job was excluded from the current active shortlist.")


def automation_state_label(state: AutomationState) -> str:
    labels = {
        AutomationState.READY: "Ready",
        AutomationState.APPLIED: "Applied",
        AutomationState.QUEUED: "Queued",
        AutomationState.NEEDS_REVIEW: "Needs review",
        AutomationState.FAILED: "Failed",
    }
    return labels[state]


def automation_state_kind(state: AutomationState) -> str | None:
    if state == AutomationState.APPLIED:
        return "signal"
    if state in {AutomationState.QUEUED, AutomationState.NEEDS_REVIEW}:
        return "warm"
    return "signal" if state == AutomationState.READY else None


def automation_note(result: JobAutomationResult) -> str:
    note_parts = [
        f"{automation_state_label(result.automation_state)} via {result.handler_name}.",
        result.auto_apply_message or f"{route_display_name(result.route)} route detected.",
    ]
    return " ".join(part.strip() for part in note_parts if part.strip())


def format_runtime(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return "-"
    return f"{duration_seconds:.1f}s"


def format_duration(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    return f" in {format_runtime(duration_seconds)}"


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
