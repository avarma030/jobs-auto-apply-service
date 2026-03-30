"""CLI entrypoint for the jobs auto-apply service."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import settings
from src.database import Database
from src.models import ExperienceLevel, JobSearchFilter, JobType, WorkMode
from src.orchestrator import Orchestrator
from src.utils import load_profile

console = Console()


def _setup_logging() -> None:
    import sys

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level, colorize=True)
    logger.add("logs/app.log", level="DEBUG", rotation="10 MB", retention="30 days")


@click.group()
def main() -> None:
    """Jobs Auto-Apply Service — scrape and apply to jobs automatically."""
    _setup_logging()


# ──────────────────────────────────────────────────────────────────────────────
# scrape
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--keywords", "-k", multiple=True, required=True, help="Job title keywords")
@click.option("--location", "-l", default=None, help="Location (city, state, or 'remote')")
@click.option("--remote/--no-remote", default=False, help="Remote jobs only")
@click.option("--max-age", default=7, help="Max job age in days")
@click.option("--salary-min", default=None, type=float, help="Minimum salary")
def scrape(
    keywords: tuple[str, ...],
    location: str | None,
    remote: bool,
    max_age: int,
    salary_min: float | None,
) -> None:
    """Scrape job boards and store results in the database."""
    search_filter = JobSearchFilter(
        keywords=list(keywords),
        location=location,
        remote_only=remote,
        max_age_days=max_age,
        salary_min=salary_min,
    )

    async def _run() -> None:
        db = Database(settings.database_url)
        await db.init()
        profile = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile, db=db, runtime_scope="cli")
        count = await orch.run_scrape(search_filter)
        console.print(f"[green]Scraped {count} new jobs.[/green]")
        await db.close()

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# apply
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dry-run/--no-dry-run", default=False, help="Preview without submitting")
def apply(dry_run: bool) -> None:
    """Apply to all pending jobs in the database."""
    if dry_run:
        import os
        os.environ["DRY_RUN"] = "true"
        settings.__init__()  # reload

    async def _run() -> None:
        db = Database(settings.database_url)
        await db.init()
        profile = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile, db=db, runtime_scope="cli")
        counts = await orch.run_apply()
        _print_counts(counts)
        await db.close()

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# run (scrape + apply in one shot)
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--keywords", "-k", multiple=True, required=True)
@click.option("--location", "-l", default=None)
@click.option("--remote/--no-remote", default=False)
@click.option("--dry-run/--no-dry-run", default=False)
@click.option(
    "--no-ai",
    is_flag=True,
    default=False,
    help="Skip AI scoring/tailoring — fall back to scrape-then-apply",
)
def run(
    keywords: tuple[str, ...],
    location: str | None,
    remote: bool,
    dry_run: bool,
    no_ai: bool,
) -> None:
    """Scrape then apply in a single command (AI pipeline by default).

    The AI pipeline:
      1. Scrapes jobs from all enabled boards
      2. Scores each job vs your resume — skips < 75% matches
      3. Tailors your resume for each qualifying job (ATS score ≥ 90%)
      4. Generates a tailored cover letter
      5. Applies via LinkedIn Easy Apply or the appropriate ATS handler

    Set ANTHROPIC_API_KEY in .env to enable AI features.
    Use --no-ai to skip scoring/tailoring and apply directly.
    """
    search_filter = JobSearchFilter(
        keywords=list(keywords),
        location=location,
        remote_only=remote,
    )

    async def _run() -> None:
        import os

        if dry_run:
            os.environ["DRY_RUN"] = "true"

        db = Database(settings.database_url)
        await db.init()
        profile = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile, db=db, runtime_scope="cli")

        if no_ai or not settings.anthropic_api_key:
            if not no_ai and not settings.anthropic_api_key:
                console.print(
                    "[yellow]⚠ ANTHROPIC_API_KEY not set — falling back to basic scrape+apply.[/yellow]\n"
                    "[dim]Set ANTHROPIC_API_KEY in .env to enable AI scoring and resume tailoring.[/dim]"
                )
            scraped = await orch.run_scrape(search_filter)
            console.print(f"[cyan]Scraped {scraped} new jobs.[/cyan]")
            if not dry_run:
                counts = await orch.run_apply()
                _print_counts(counts)
        else:
            console.rule("[bold cyan]AI-Powered Job Pipeline[/bold cyan]")
            counts = await orch.run_full_pipeline(
                search_filter,
                progress_callback=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            )
            _print_counts(counts)

        await db.close()

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# extract-profile
# ──────────────────────────────────────────────────────────────────────────────

@main.command("extract-profile")
@click.option(
    "--resume", "resume_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to resume PDF (defaults to RESUME_PATH in .env)",
)
@click.option(
    "--output", "output_path",
    default=None,
    type=click.Path(),
    help="Where to save the extracted profile JSON (defaults to USER_PROFILE_PATH in .env)",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite existing profile. Default is to merge (extracted fills empty fields; existing wins where set).",
)
def extract_profile(
    resume_path: str | None,
    output_path: str | None,
    overwrite: bool,
) -> None:
    """Extract a structured profile from your resume PDF using Claude.

    This is the recommended first step — upload your resume and the system
    will automatically populate your profile (name, skills, work history,
    education, social links) so you don't have to fill anything in manually.

    Requires ANTHROPIC_API_KEY to be set in .env.

    Example:
        python main.py extract-profile
        python main.py extract-profile --resume ~/Downloads/cv.pdf --overwrite
    """
    async def _run() -> None:
        import anthropic as _anthropic
        from rich.table import Table
        from src.services import profile_extractor, resume_parser
        from src.utils import save_profile

        if not settings.anthropic_api_key:
            console.print(
                "[red]✗ ANTHROPIC_API_KEY not set.[/red]\n"
                "[dim]Add it to your .env file and try again.[/dim]"
            )
            raise SystemExit(1)

        resume = Path(resume_path) if resume_path else settings.resume_path
        output = Path(output_path) if output_path else settings.user_profile_path

        if not resume.exists():
            console.print(f"[red]✗ Resume not found at {resume}[/red]")
            raise SystemExit(1)

        console.rule("[bold cyan]Extracting profile from resume[/bold cyan]")
        console.print(f"  Resume:  [cyan]{resume}[/cyan]")
        console.print(f"  Output:  [cyan]{output}[/cyan]")
        console.print(f"  Mode:    [cyan]{'overwrite' if overwrite else 'merge'}[/cyan]\n")

        # Parse + extract
        console.print("[bold]1. Parsing resume …[/bold]")
        resume_text = resume_parser.parse_resume(resume)
        console.print(f"   ✓ {len(resume_text)} characters extracted")

        console.print("\n[bold]2. Asking Claude to extract profile …[/bold]")
        client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        extracted = await profile_extractor.extract_profile_from_resume(
            resume_text, client, settings.anthropic_model
        )

        if not extracted:
            console.print("[red]✗ Extraction returned no data — check the resume format.[/red]")
            raise SystemExit(1)

        # Merge or overwrite
        if not overwrite and output.exists():
            console.print("\n[bold]3. Merging with existing profile …[/bold]")
            try:
                existing_profile = load_profile(output)
                existing = existing_profile.model_dump()
                for key, val in extracted.items():
                    if key not in existing or not existing[key]:
                        existing[key] = val
                final = existing
            except Exception:
                final = extracted
        else:
            console.print("\n[bold]3. Writing new profile …[/bold]")
            final = extracted

        # Save
        from src.models import UserProfile as _UserProfile
        profile_obj = _UserProfile(**{k: v for k, v in final.items() if v is not None})
        save_profile(profile_obj, output)

        # Summary table
        console.print()
        console.rule("[bold]Extracted Profile Summary[/bold]")
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold cyan", min_width=22)
        table.add_column()

        name = f"{extracted.get('first_name', '')} {extracted.get('last_name', '')}".strip()
        table.add_row("Name", name or "–")
        table.add_row("Email", extracted.get("email") or "–")
        table.add_row("Phone", extracted.get("phone") or "–")
        table.add_row("Headline", extracted.get("headline") or "–")
        table.add_row("Years experience", str(extracted.get("years_of_experience") or "–"))
        skills = extracted.get("skills") or []
        table.add_row("Skills", ", ".join(skills[:8]) + ("…" if len(skills) > 8 else "") if skills else "–")
        jobs = extracted.get("work_experience") or []
        table.add_row("Work history", f"{len(jobs)} entries")
        edu = extracted.get("education") or []
        table.add_row("Education", f"{len(edu)} entries")
        links = extracted.get("social_links") or {}
        table.add_row("LinkedIn", links.get("linkedin") or "–")
        table.add_row("GitHub", links.get("github") or "–")

        console.print(table)
        console.print()
        console.print(f"[green]✓ Profile saved to {output}[/green]")
        console.print(
            "[dim]Review and edit the file, then set your job board credentials "
            "before running: python main.py run[/dim]"
        )

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# stats
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
def stats() -> None:
    """Show application statistics."""

    async def _run() -> None:
        db = Database(settings.database_url)
        await db.init()
        data = await db.get_application_stats()
        table = Table(title="Application Statistics")
        table.add_column("Status", style="bold")
        table.add_column("Count", justify="right")
        for status, count in sorted(data.items()):
            table.add_row(status, str(count))
        console.print(table)
        await db.close()

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# login
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("board", type=click.Choice(["linkedin"], case_sensitive=False))
def login(board: str) -> None:
    """Open a visible browser window to log in and save the session cookie.

    Run this once before using the applier so it doesn't need to log in
    on every run.  The session is saved to data/.linkedin_session/.

    Example:
        python main.py login linkedin
    """
    async def _run() -> None:
        from pathlib import Path
        from src.utils.browser import BrowserManager

        console.print(f"\n[bold cyan]Opening browser for {board} login…[/bold cyan]")
        console.print("[dim]Log in manually, then close the browser window (or press Ctrl-C).[/dim]\n")

        bm = BrowserManager(
            headless=False,
            user_data_dir=Path(f"data/.{board}_session"),
        )
        await bm.start()
        page = await bm.new_page()

        if board == "linkedin":
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        # Keep browser open until user closes it or Ctrl-C
        try:
            await page.wait_for_event("close", timeout=0)  # wait forever
        except Exception:
            pass
        finally:
            await bm.stop()

        console.print(f"[green]✓ Session saved to data/.{board}_session/[/green]")
        console.print("[dim]Future runs will reuse this session automatically.[/dim]")

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# verify-linkedin
# ──────────────────────────────────────────────────────────────────────────────

@main.command("verify-linkedin")
@click.option("--keywords", "-k", default="software engineer", help="Test search keywords")
@click.option("--max-jobs", default=5, type=int, help="Jobs to fetch for the health check")
def verify_linkedin(keywords: str, max_jobs: int) -> None:
    """Run a quick health check on the LinkedIn integration.

    Checks:
    • Can reach the LinkedIn guest search API
    • Parses at least one job card
    • Session cookie exists (login status)

    Does NOT apply to any jobs.
    """
    async def _run() -> None:
        import sys
        from src.models import JobSearchFilter
        from src.scrapers.linkedin import LinkedInScraper
        from rich.panel import Panel

        console.rule("[bold cyan]LinkedIn Health Check[/bold cyan]")
        results: dict[str, str] = {}

        # 1. Scraper connectivity
        console.print("\n[bold]1. Scraping test…[/bold]")
        jobs = []
        error_msg = ""
        try:
            async with LinkedInScraper() as scraper:
                search_filter = JobSearchFilter(
                    keywords=keywords.split(), max_age_days=0
                )
                async for job in scraper.search(search_filter):
                    jobs.append(job)
                    console.print(f"   ✓ {job.title} @ {job.company}")
                    if len(jobs) >= max_jobs:
                        break
        except Exception as exc:
            error_msg = str(exc)

        if jobs:
            results["Scraping"] = f"[green]✓ {len(jobs)} jobs fetched[/green]"
        else:
            results["Scraping"] = f"[red]✗ No jobs (error: {error_msg or 'empty response'})[/red]"

        # 2. Session cookie
        console.print("\n[bold]2. Session cookie…[/bold]")
        session_dir = Path("data/.linkedin_session")
        if session_dir.exists() and any(session_dir.iterdir()):
            results["Session cookie"] = "[green]✓ Found[/green]"
            console.print("   ✓ Session directory exists")
        else:
            results["Session cookie"] = "[yellow]⚠ Not found — run: python main.py login linkedin[/yellow]"
            console.print("   ⚠ No session — run 'python main.py login linkedin' to log in")

        # 3. Resume file
        console.print("\n[bold]3. Resume file…[/bold]")
        resume = Path(settings.resume_path)
        if resume.exists():
            results["Resume"] = f"[green]✓ {resume}[/green]"
            console.print(f"   ✓ {resume}")
        else:
            results["Resume"] = f"[yellow]⚠ Not found at {resume}[/yellow]"
            console.print(f"   ⚠ Missing — add your resume at {resume}")

        # 4. User profile
        console.print("\n[bold]4. User profile…[/bold]")
        profile_path = Path(settings.user_profile_path)
        if profile_path.exists():
            try:
                profile = load_profile(profile_path)
                results["User profile"] = f"[green]✓ {profile.first_name} {profile.last_name}[/green]"
                console.print(f"   ✓ Loaded profile for {profile.first_name} {profile.last_name}")
            except Exception as exc:
                results["User profile"] = f"[red]✗ Parse error: {exc}[/red]"
        else:
            results["User profile"] = f"[yellow]⚠ Not found — copy data/user_profile.example.json[/yellow]"
            console.print("   ⚠ Missing — copy data/user_profile.example.json to data/user_profile.json")

        # 5. Anthropic API key (AI features)
        console.print("\n[bold]5. Anthropic API key (AI pipeline)…[/bold]")
        if settings.anthropic_api_key:
            import anthropic as _anthropic
            try:
                client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                # Lightweight ping: list models endpoint requires a valid key
                msg = await client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "hi"}],
                )
                results["Anthropic API"] = "[green]✓ Key valid — AI pipeline enabled[/green]"
                console.print("   ✓ API key valid")
            except Exception as exc:
                results["Anthropic API"] = f"[red]✗ API error: {exc}[/red]"
                console.print(f"   ✗ API error: {exc}")
        else:
            results["Anthropic API"] = (
                "[yellow]⚠ Not set — AI scoring/tailoring disabled[/yellow]\n"
                "             Set ANTHROPIC_API_KEY in .env to enable"
            )
            console.print("   ⚠ ANTHROPIC_API_KEY not set — AI features disabled (jobs will apply without scoring/tailoring)")

        # Summary
        console.print()
        console.rule("[bold]Summary[/bold]")
        for check, status in results.items():
            console.print(f"  {check:20s} {status}")

        all_green = all("✓" in v for v in results.values())
        console.print()
        if all_green:
            console.print(Panel("[bold green]All checks passed — LinkedIn is ready.[/bold green]", expand=False))
        else:
            console.print(Panel("[bold yellow]Some checks need attention (see above).[/bold yellow]", expand=False))

    asyncio.run(_run())


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _print_counts(counts: dict) -> None:
    table = Table(title="Application Results")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    for status, count in sorted(counts.items()):
        color = "green" if status == "applied" else "yellow" if status == "skipped" else "red"
        table.add_row(f"[{color}]{status}[/{color}]", str(count))
    console.print(table)


if __name__ == "__main__":
    main()
