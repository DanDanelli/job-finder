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
   Himalayas, JSearch, Adzuna, We Work Remotely),
   normalizes and deduplicates them, applies the profile's hard filters and
   ranks the rest with BM25.
3. The best candidates are scored by Claude (fit 0-100, strengths, gaps,
   dealbreakers).
4. The Telegram bot sends the digest at the scheduled times, with
   👍 Save / 👎 Dismiss buttons. `/evaluate <link>` scores a single job you found
   elsewhere.
5. Your decisions teach it. When a job is scored, the saved and dismissed jobs
   most like it go into the prompt, so Claude calibrates to your taste. Once a
   week Claude also reads all your decisions and proposes rules ("dismisses
   outsourcing consultancies"); they count only after you approve or edit them
   on the site.

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

The app copies the database every day at 03:30 (and on start when today's copy
is missing), keeping the last 7 days (`JOBFINDER_BACKUP_TIME`,
`JOBFINDER_BACKUP_KEEP_DAYS`). The copies go to `./data/backup` unless you set
`BACKUP_HOST_DIR` in `.env` to a folder on another disk, which is what you want:
a copy on the same disk dies with it. `job-finder db backup` makes one now. To
restore, stop the container and copy a `job_finder-<date>.db` over
`data/job_finder.db`.

## Site

Each profile signs in with its own name and password and sees only its own
data. There is no sign-up: the first password is set from the terminal; after
that, each person can change their own under **Account** (the other devices
are signed out).

```bash
docker compose exec job-finder job-finder profile password <name>
# or, without an interactive terminal (e.g. piped over ssh):
printf '%s\n' "$PASSWORD" | docker compose exec -T job-finder job-finder profile password <name> --stdin
```

Pages:

- **Jobs**: New / Saved / Dismissed tabs, minimum fit and work mode filters,
  sorted by fit or by posting date (either way); save, dismiss, restore.
  **Search now** runs a search for your profile in the background.
- **Profile**: searches, filters, preferences for Claude, pause. Tightening a
  filter removes the matches it now rejects that nobody has seen yet (not sent
  in a digest, no feedback); loosening one takes effect at the next run.
- **Learned from your feedback** (linked from Profile): the rules Claude
  proposed from your saved and dismissed jobs, to approve, edit or discard;
  **Suggest rules now** asks for a fresh proposal (after at least 5 decisions).
  On Telegram you get a note when a proposal is waiting (with a link if
  `JOBFINDER_SITE_URL` is set).
- **CV**: upload a PDF (up to 10 MB); Claude reads it in the background and
  you review what it extracted (summary, roles, seniority, skills, languages,
  industries, certifications, suggested searches) before anything changes.
  The same form edits that data at any time.
- **Status**: what a running search is doing (the page refreshes itself), how
  the last one went, each source's requests today and this month against its
  limits, recent runs with their warnings, and Claude's spend this month
  (total, per use, and yours).

The pages are plain HTML forms; no JavaScript. The container listens only on
`127.0.0.1:8000`. Reach it over Tailscale, which also gives HTTPS (the session
cookie is `Secure` by default):

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8000
# https://<machine>.<tailnet>.ts.net:8443
```

Someone outside your tailnet can get in if you share just that machine with
them from the Tailscale admin console (Machines → ⋯ → Share); they use the
full `<machine>.<tailnet>.ts.net` name. For plain HTTP on a trusted network,
set `JOBFINDER_WEB_SECURE_COOKIE=false`.

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
- **We Work Remotely:** the public RSS feeds, one request per category per run;
  every job links back to We Work Remotely, as its feed page asks.

Sources whose terms forbid aggregation are not supported.
