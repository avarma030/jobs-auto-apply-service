"""
LinkedIn End-to-End Live Test
=============================
Runs the real scraper against LinkedIn and prints every result.
Does NOT apply to any jobs.

Usage
-----
    python test_live_linkedin.py

    # Narrow the search
    python test_live_linkedin.py --keywords "backend engineer" --location "New York" --max-jobs 10

    # Also fetch full job descriptions (slower — one extra request per job)
    python test_live_linkedin.py --keywords "python developer" --fetch-details

    # Remote jobs only
    python test_live_linkedin.py --keywords "software engineer" --remote

Options
-------
    --keywords   TEXT   Job title / skill keywords  [default: software engineer]
    --location   TEXT   City, state or country       [default: United States]
    --remote             Remote jobs only
    --max-jobs   INT    Stop after N jobs            [default: 25]
    --max-age    INT    Max job age in days           [default: 7]
    --fetch-details     Also fetch full description + Easy Apply detection
    --save-html         Dump raw search HTML to debug/page_<n>.html for selector debugging
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Make sure the project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from src.models import JobSearchFilter
from src.scrapers.linkedin import LinkedInScraper

console = Console()


async def run(
    keywords: list[str],
    location: str,
    remote: bool,
    max_jobs: int,
    max_age: int,
    fetch_details: bool,
    save_html: bool,
) -> None:
    search_filter = JobSearchFilter(
        keywords=keywords,
        location=location if not remote else None,
        remote_only=remote,
        max_age_days=max_age,
    )

    console.rule("[bold cyan]LinkedIn Live Scrape Test[/bold cyan]")
    console.print(f"  Keywords : [yellow]{' '.join(keywords)}[/yellow]")
    if remote:
        console.print("  Work mode: [yellow]Remote only[/yellow]")
    elif location:
        console.print(f"  Location : [yellow]{location}[/yellow]")
    console.print(f"  Max age  : [yellow]{max_age} days[/yellow]")
    console.print(f"  Max jobs : [yellow]{max_jobs}[/yellow]")
    console.print(f"  Details  : [yellow]{fetch_details}[/yellow]")
    console.print()

    if save_html:
        Path("debug").mkdir(exist_ok=True)

    jobs = []
    errors: list[str] = []

    async with LinkedInScraper() as scraper:
        # Patch to optionally save raw HTML for debugging
        original_fetch = scraper._fetch_search_page
        page_counter = [0]

        async def patched_fetch(params):
            html = await original_fetch(params)
            if save_html and html:
                page_counter[0] += 1
                out = Path(f"debug/page_{page_counter[0]}.html")
                out.write_text(html)
                console.print(f"  [dim]Saved raw HTML → {out}[/dim]")
            return html

        scraper._fetch_search_page = patched_fetch

        console.print("[bold]Scraping...[/bold]")
        start = datetime.utcnow()

        try:
            async for job in scraper.search(search_filter):
                jobs.append(job)
                status_icon = "🟢" if job.easy_apply else "🔵"
                console.print(
                    f"  {status_icon} [{len(jobs):>3}] "
                    f"[bold]{job.title}[/bold] @ {job.company}"
                    f"{'  [dim]' + job.location + '[/dim]' if job.location else ''}"
                )

                if fetch_details and not job.description:
                    try:
                        job = await scraper.get_job_details(job)
                        if job.easy_apply:
                            console.print(f"         [green]✓ Easy Apply[/green]")
                    except Exception as exc:
                        errors.append(f"Detail fetch failed for {job.external_id}: {exc}")

                if len(jobs) >= max_jobs:
                    console.print(f"\n  [dim]Reached --max-jobs {max_jobs} limit, stopping.[/dim]")
                    break

        except Exception as exc:
            console.print(f"\n[red bold]Scraper error:[/red bold] {exc}")
            import traceback
            traceback.print_exc()
            errors.append(str(exc))

    elapsed = (datetime.utcnow() - start).total_seconds()

    # ── Summary table ────────────────────────────────────────────────────────
    console.print()
    console.rule("[bold]Results[/bold]")

    if not jobs:
        console.print("[red]No jobs found.[/red]")
        console.print("\nPossible reasons:")
        console.print("  • LinkedIn blocked the request (try again in a few minutes)")
        console.print("  • Selectors are stale — run with [bold]--save-html[/bold] and inspect debug/page_1.html")
        console.print("  • No jobs match the filter (try broader keywords)")
        return

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Title", min_width=25)
    table.add_column("Company", min_width=20)
    table.add_column("Location", min_width=20)
    table.add_column("Posted", width=12)
    table.add_column("Easy Apply", width=10, justify="center")
    table.add_column("URL", overflow="fold", min_width=20)

    easy_apply_count = 0
    for i, job in enumerate(jobs, 1):
        ea = "✓" if job.easy_apply else ""
        if job.easy_apply:
            easy_apply_count += 1
        posted = ""
        if job.posted_at:
            delta = datetime.utcnow() - job.posted_at.replace(tzinfo=None)
            posted = f"{delta.days}d ago" if delta.days > 0 else "today"

        table.add_row(
            str(i),
            job.title,
            job.company,
            job.location or "",
            posted,
            f"[green]{ea}[/green]" if ea else "",
            job.url,
        )

    console.print(table)

    # ── Stats ────────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"  Jobs scraped : [bold green]{len(jobs)}[/bold green]\n"
        f"  Easy Apply   : [bold green]{easy_apply_count}[/bold green] "
        f"([dim]{easy_apply_count * 100 // len(jobs)}%[/dim])\n"
        f"  Elapsed      : [dim]{elapsed:.1f}s[/dim]",
        title="Summary",
        expand=False,
    ))

    if errors:
        console.print()
        console.print(f"[yellow]Errors ({len(errors)}):[/yellow]")
        for e in errors:
            console.print(f"  [red]•[/red] {e}")

    # ── Save JSON ────────────────────────────────────────────────────────────
    out_path = Path("debug/scraped_jobs.json")
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("w") as f:
        json.dump(
            [
                {
                    "id": job.external_id,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "url": job.url,
                    "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                    "easy_apply": job.easy_apply,
                    "job_type": job.job_type,
                    "experience_level": job.experience_level,
                    "salary_min": job.salary_min,
                    "salary_max": job.salary_max,
                    "description": (job.description[:300] + "...") if job.description else None,
                }
                for job in jobs
            ],
            f,
            indent=2,
        )
    console.print(f"\n[dim]Full results saved → {out_path}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live LinkedIn scrape test — no applications submitted"
    )
    parser.add_argument(
        "--keywords", nargs="+", default=["software engineer"],
        metavar="KEYWORD",
        help="Search keywords (default: 'software engineer')",
    )
    parser.add_argument(
        "--location", default="United States",
        help="Location string (default: 'United States')",
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Remote jobs only",
    )
    parser.add_argument(
        "--max-jobs", type=int, default=25,
        metavar="N",
        help="Stop after N jobs (default: 25)",
    )
    parser.add_argument(
        "--max-age", type=int, default=7,
        metavar="DAYS",
        help="Max job age in days (default: 7, use 0 to disable)",
    )
    parser.add_argument(
        "--fetch-details", action="store_true",
        help="Fetch full job description + detect Easy Apply for each job",
    )
    parser.add_argument(
        "--save-html", action="store_true",
        help="Save raw search HTML pages to debug/ for selector debugging",
    )
    args = parser.parse_args()

    # Configure logging — only show WARNING+ in console to keep output clean
    logger.remove()
    logger.add(sys.stderr, level="WARNING", colorize=True)
    logger.add("debug/scrape.log", level="DEBUG", rotation="5 MB")
    Path("debug").mkdir(exist_ok=True)

    asyncio.run(run(
        keywords=args.keywords,
        location=args.location,
        remote=args.remote,
        max_jobs=args.max_jobs,
        max_age=args.max_age,
        fetch_details=args.fetch_details,
        save_html=args.save_html,
    ))


if __name__ == "__main__":
    main()
