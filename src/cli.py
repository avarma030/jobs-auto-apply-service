"""CLI entrypoint for the jobs auto-apply service."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import settings
from src.dashboard import DEFAULT_HOST, DEFAULT_PORT, serve_dashboard

console = Console()


def _setup_logging() -> None:
    import sys

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level, colorize=True)
    logger.add("logs/app.log", level="DEBUG", rotation="10 MB", retention="30 days")


@click.group()
def main() -> None:
    """Jobs Auto-Apply Service - scrape and apply to jobs automatically."""
    _setup_logging()


@main.group()
def profile() -> None:
    """Create or update the local user profile."""


@profile.command("bootstrap")
@click.option(
    "--resume",
    "resume_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=settings.resume_path,
    show_default=True,
    help="Resume file to parse (PDF, DOCX, or TXT)",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=settings.user_profile_path,
    show_default=True,
    help="Where to write the generated profile JSON",
)
@click.option(
    "--force/--no-force",
    default=False,
    show_default=True,
    help="Overwrite the target profile if it already exists",
)
def profile_bootstrap(resume_path: Path, output_path: Path, force: bool) -> None:
    """Generate a user profile draft from a resume."""
    from src.utils.profile_bootstrap import bootstrap_profile_from_resume

    try:
        result = bootstrap_profile_from_resume(
            resume_path=resume_path,
            output_path=output_path,
            overwrite=force,
        )
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(f"[green]Generated profile draft at {result.output_path}[/green]")
    console.print(f"[cyan]Resume source:[/cyan] {resume_path}")

    if result.extracted_fields:
        console.print("[bold]Extracted fields[/bold]")
        for field_name in result.extracted_fields:
            console.print(f"  - {field_name}")

    if result.review_notes:
        console.print("\n[bold yellow]Review before running autopilot[/bold yellow]")
        for note in result.review_notes:
            console.print(f"  - {note}")
    else:
        console.print("\n[green]No obvious gaps were detected in the generated draft.[/green]")


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
    from src.models import JobSearchFilter

    search_filter = JobSearchFilter(
        keywords=list(keywords),
        location=location,
        remote_only=remote,
        max_age_days=max_age,
        salary_min=salary_min,
    )

    async def _run() -> None:
        from src.database import Database
        from src.orchestrator import Orchestrator
        from src.utils import load_profile

        db = Database(settings.database_url)
        await db.init()
        profile_data = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile_data, db=db)
        count = await orch.run_scrape(search_filter)
        console.print(f"[green]Scraped {count} new jobs.[/green]")
        await db.close()

    asyncio.run(_run())


@main.command()
@click.option("--dry-run/--no-dry-run", default=False, help="Preview without submitting")
def apply(dry_run: bool) -> None:
    """Apply to all pending jobs in the database."""
    if dry_run:
        import os

        os.environ["DRY_RUN"] = "true"
        settings.__init__()

    async def _run() -> None:
        from src.database import Database
        from src.orchestrator import Orchestrator
        from src.utils import load_profile

        db = Database(settings.database_url)
        await db.init()
        profile_data = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile_data, db=db)
        counts = await orch.run_apply()
        _print_counts(counts)
        await db.close()

    asyncio.run(_run())


@main.command()
@click.option("--keywords", "-k", multiple=True, required=True)
@click.option("--location", "-l", default=None)
@click.option("--remote/--no-remote", default=False)
@click.option("--dry-run/--no-dry-run", default=False)
def run(
    keywords: tuple[str, ...],
    location: str | None,
    remote: bool,
    dry_run: bool,
) -> None:
    """Scrape then apply in a single command."""
    from src.models import JobSearchFilter

    search_filter = JobSearchFilter(
        keywords=list(keywords),
        location=location,
        remote_only=remote,
    )

    async def _run() -> None:
        from src.database import Database
        from src.orchestrator import Orchestrator
        from src.utils import load_profile

        db = Database(settings.database_url)
        await db.init()
        profile_data = load_profile(settings.user_profile_path)
        orch = Orchestrator(profile=profile_data, db=db)
        scraped = await orch.run_scrape(search_filter)
        console.print(f"[cyan]Scraped {scraped} new jobs.[/cyan]")
        if not dry_run:
            counts = await orch.run_apply()
            _print_counts(counts)
        await db.close()

    asyncio.run(_run())


@main.command()
def stats() -> None:
    """Show application statistics."""

    async def _run() -> None:
        from src.database import Database

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


@main.command()
@click.argument("board", type=click.Choice(["linkedin"], case_sensitive=False))
def login(board: str) -> None:
    """Open a visible browser window to bootstrap a saved board session."""

    async def _run() -> None:
        from src.utils.browser import BrowserManager

        session_dir = Path(f"data/.{board}_session")
        console.print(f"\n[bold cyan]Opening browser for {board} login...[/bold cyan]")
        console.print("[dim]Log in manually, then close the tab or press Ctrl+C when you're done.[/dim]\n")

        manager = BrowserManager(headless=False, user_data_dir=session_dir)
        await manager.start()
        page = await manager.new_page()

        if board == "linkedin":
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            await manager.stop()

        console.print(f"[green]Session saved to {session_dir}[/green]")
        console.print("[dim]Future LinkedIn autopilot runs will reuse this session.[/dim]")

    asyncio.run(_run())


@main.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Host to bind the dashboard server")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int, help="Port to bind the dashboard server")
@click.option(
    "--open-browser/--no-open-browser",
    default=True,
    show_default=True,
    help="Open the dashboard in your browser automatically",
)
def dashboard(host: str, port: int, open_browser: bool) -> None:
    """Launch the local autopilot dashboard."""
    console.print(f"[cyan]Starting dashboard at http://{host}:{port}[/cyan]")
    console.print("[dim]Press Ctrl+C to stop the local server.[/dim]")
    try:
        serve_dashboard(host=host, port=port, open_browser=open_browser)
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc


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
