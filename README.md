# Job Finder

A personal assistant that reads your CV, collects jobs from several sources,
ranks them by fit with Claude and sends a daily digest on Telegram. Built to run
on a Raspberry Pi.

- Design: [docs/plans/2026-11-15-job-finder-design.md](docs/plans/2026-11-15-job-finder-design.md)
- Plan: [docs/plans/2026-11-15-job-finder-implementation.md](docs/plans/2026-11-15-job-finder-implementation.md)

## How it works

1. `job-finder profile import cv.pdf -p <name>` reads the CV with Claude and
   fills in the profile (skills, roles, suggested search queries).
2. Each run collects jobs from the enabled sources (Remotive, Remote OK,
   Himalayas, JSearch, Adzuna),
   normalizes and deduplicates them, applies the profile's hard filters and
   ranks the rest with BM25.
3. The best candidates are scored by Claude (fit 0-100, strengths, gaps,
   dealbreakers).
4. The Telegram bot sends the digest at the scheduled times, with
   👍 Save / 👎 Dismiss buttons. `/evaluate <link>` scores a single job you found
   elsewhere.

Bot messages and Claude's text follow `JOBFINDER_LANGUAGE` (`en` or `pt-BR`).

## Development

```bash
uv sync                      # install dependencies
cp .env.example .env         # adjust the variables
uv run job-finder db upgrade # create/upgrade the database
cp profiles/example.yaml profiles/me.yaml   # edit it, then:
uv run job-finder profile load profiles/me.yaml
uv run job-finder run --dry-run             # collect and filter, writing nothing
uv run job-finder serve      # app, bot and scheduler on http://localhost:8000
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Raspberry Pi (Docker)

```bash
cp .env.example .env
docker compose up -d --build
```

The database lives in the `./data` volume. Migrations run on startup.

## Sources and their terms

Each connector follows its source's terms, recorded in its docstring:

- **Remotive:** at most 4 calls a day; every job links back to and credits
  Remotive.
- **Remote OK:** one call per run; every job links back to and credits Remote OK.
- **Himalayas:** a few searches per run (an optional free key raises the
  limit); jobs open through Himalayas' application link, credit Himalayas, and
  expired jobs are dropped. Jobs are not passed on to third parties.
- **JSearch:** stays within the plan's monthly quota.
- **Adzuna:** personal job search only; every job credits Adzuna with a link,
  and Adzuna's salary estimates are not shown. Its key travels in the URL, so
  errors are reported without it.

Sources whose terms forbid aggregation are not supported.
