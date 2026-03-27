# Jobs Auto-Apply Service

Automated job scraping and application service. Scrapes listings from multiple job boards and automatically applies using your profile data and browser automation.

## Supported Job Boards

| Board | Scraping | Auto-Apply | Status |
|---|---|---|---|
| LinkedIn | Guest + Auth | Easy Apply | Planned |
| Indeed | Guest | Instant Apply | Planned |
| Glassdoor | Auth required | Via redirect | Planned |
| ZipRecruiter | Guest | N/A | Planned |
| Dice | Public API | N/A | Planned |
| Monster | Guest | N/A | Planned |
| Lever (ATS) | Public API | Form automation | Planned |
| Greenhouse (ATS) | Public API | Form automation | Planned |
| Workday (ATS) | JSON API | Form automation | Planned |

## Architecture

```
jobs-auto-apply-service/
├── src/
│   ├── scrapers/          # One scraper class per job board
│   ├── appliers/          # One applier class per ATS / board
│   ├── models/            # Pydantic data models (Job, UserProfile, …)
│   ├── database/          # SQLAlchemy async DB layer
│   ├── config/            # Settings loaded from .env
│   ├── utils/             # Profile loader, helpers
│   ├── orchestrator.py    # Coordinates scraping + applying
│   └── cli.py             # Click CLI entrypoint
├── data/
│   ├── user_profile.example.json   # Template — copy and fill in
│   └── resume.pdf                  # Your resume (add manually)
├── tests/
├── Dockerfile
├── docker-compose.yml
└── main.py
```

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Set up your profile

```bash
cp data/user_profile.example.json data/user_profile.json
# Edit data/user_profile.json with your info
# Add your resume as data/resume.pdf
cp .env.example .env
# Edit .env as needed
```

### 3. Scrape jobs

```bash
python main.py scrape --keywords "software engineer" --remote
python main.py scrape -k "backend engineer" -k "python" --location "New York"
```

### 4. Apply to scraped jobs

```bash
# Preview without submitting
python main.py apply --dry-run

# Actually apply
python main.py apply
```

### 5. Scrape + apply in one shot

```bash
python main.py run --keywords "senior python developer" --remote
```

### 6. View stats

```bash
python main.py stats
```

### 7. Run the local dashboard

```bash
python main.py dashboard
```

Then open `http://127.0.0.1:8765` in your browser. The dashboard runs the live LinkedIn scraper and renders results from a search form instead of the CLI.

## Docker

```bash
# Copy and configure your .env
cp .env.example .env

# Run with docker-compose
docker compose up app
```

## Configuration

All configuration is via `.env` (see `.env.example` for full reference).

Key settings:

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `false` | Scrape but don't submit applications |
| `ENABLED_BOARDS` | `all` | Comma-separated board slugs to enable |
| `MAX_APPLICATIONS_PER_RUN` | `50` | Cap on applications per run |
| `HEADLESS_BROWSER` | `true` | Run browser in headless mode |
| `REQUEST_DELAY_SECONDS` | `2.0` | Delay between requests (be polite) |
| `WORKDAY_TENANT_URLS` | empty | Comma-separated public Workday board URLs to scrape |
| `DATABASE_URL` | SQLite | Use PostgreSQL URL for production |

## User Profile

Copy `data/user_profile.example.json` to `data/user_profile.json` and fill in:

- Personal info (name, email, phone, address)
- Work experience and education
- Skills list
- Job board account credentials
- Application preferences (max per day, blacklisted companies, etc.)
- Custom answers to common screening questions

## Development

```bash
# Run tests
pytest

# Lint
ruff check src/

# Type check
mypy src/
```

## Roadmap

- [ ] LinkedIn scraper + Easy Apply automation
- [ ] Indeed scraper + Instant Apply automation
- [ ] Glassdoor scraper
- [ ] Lever + Greenhouse + Workday ATS appliers
- [ ] Cover letter generation (LLM-powered, per-job)
- [ ] Job relevance scoring / filtering
- [ ] Notification system (email / Slack)
- [ ] Scheduled runs via APScheduler / Celery
- [ ] Web dashboard for tracking applications
