# Job Finder

A personal assistant that reads your CV, collects jobs from several sources,
ranks them by fit with Claude and sends a daily digest on Telegram. Several people can
share one installation, each with their own profile, filters and chat. It runs in a
single Docker container with SQLite, so a small machine is enough.

**Setting it up:** [docs/setup.md](docs/setup.md), step by step from zero to your first digest.

## How it works

1. You upload your CV on the site; Claude reads it and fills in the profile (skills,
   roles, suggested search queries) for you to review.
2. Each run collects jobs from the enabled sources (Remotive, Remote OK,
   Himalayas, JSearch, Adzuna, We Work Remotely),
   normalizes and deduplicates them, applies the profile's hard filters and
   ranks the rest with BM25.
3. The best candidates are scored by Claude (fit 0-100, strengths, gaps,
   dealbreakers). Scheduled runs send them in one batch (half the price) and
   wait for it up to `JOBFINDER_BATCH_MAX_WAIT_MINUTES` (30); what has not come
   back by then is scored directly.
4. The Telegram bot sends the digest at the scheduled times, with
   👍 Save / 👎 Dismiss buttons. `/evaluate <link>` scores a single job you found
   elsewhere.
5. Your decisions teach it. When a job is scored, the saved and dismissed jobs
   most like it go into the prompt, so Claude calibrates to your taste. Once a
   week Claude also reads all your decisions and proposes rules ("dismisses
   outsourcing consultancies"); they count only after you approve or edit them
   on the site. A note on why you saved or dismissed a job helps the most: type
   it in the field on the job's card on the site, or reply to the job's message
   on Telegram.

Bot messages and Claude's text follow `JOBFINDER_LANGUAGE` (`en` or `pt-BR`).

To check whether the scoring model fits your taste, `job-finder eval -p <name>`
scores your saved and dismissed jobs again with Haiku and Opus (`--model` to
choose) and shows how well each one tells them apart (AUC, mean scores, what
the digest would have shown) and what it cost. It prints an estimate and asks
before spending; `--limit 5` tries a small sample first. Nothing in your jobs
changes.

## Running it

Everything runs in one Docker container: the scheduled job collection, the Telegram
bot and the site. Any machine with Docker works (amd64 or arm64): a home
server, a small VPS, a single-board computer.

```bash
git clone https://github.com/DanDanelli/job-finder.git && cd job-finder
mkdir -p data
cp .env.example .env        # at least ANTHROPIC_API_KEY and JOBFINDER_TELEGRAM_BOT_TOKEN
docker compose up -d --build
```

Then create a profile and finish it on the site: upload your CV, review the searches and
filters, link Telegram. [docs/setup.md](docs/setup.md) walks through it.

```bash
docker compose exec job-finder job-finder profile create <name>   # asks for a password
```

The database lives in `./data` and migrations run on startup. The app copies the
database every day at 03:30 (and on start when today's copy is missing), keeping the
last 7 days (`JOBFINDER_BACKUP_TIME`, `JOBFINDER_BACKUP_KEEP_DAYS`). The copies go to
`./data/backup` unless `BACKUP_HOST_DIR` in `.env` points to a folder on another disk,
which is what you want: a copy on the same disk dies with it. `job-finder db backup`
makes one now. To restore, stop the container and copy a `job_finder-<date>.db` over
`data/job_finder.db`.

## Configuration

All settings are environment variables with the `JOBFINDER_` prefix, read from
`.env`; [`.env.example`](.env.example) lists them with their defaults. The ones you
are most likely to change:

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Claude API key (required) |
| `JOBFINDER_TELEGRAM_BOT_TOKEN` | | Bot token from @BotFather (required for Telegram) |
| `JOBFINDER_LANGUAGE` | `en` | Bot messages and Claude's text: `en` or `pt-BR` |
| `JOBFINDER_TIMEZONE` | `America/Sao_Paulo` | Time zone of the scheduled runs |
| `JOBFINDER_RUN_TIMES` | `["08:00","18:00"]` | When the scheduled runs happen |
| `JOBFINDER_DIGEST_MIN_SCORE` | `70` | Minimum fit to reach Telegram |
| `JOBFINDER_MONTHLY_COST_CAP_USD` | `5` | Claude spend cap per month |
| `JOBFINDER_CONNECTORS` | `["remotive"]` | Which job sources run (see [Sources](#sources-and-their-terms)) |
| `JOBFINDER_SITE_URL` | | The site's public address, for links in Telegram |
| `BIND_ADDRESS` | `127.0.0.1` | Read by Docker Compose: where the site's port is published |

Each person is a profile: search terms, hard filters (countries, remote eligibility,
work modes, contract types, minimum salary, blocked companies and keywords) and free-text
preferences for Claude, all edited on the site. Profiles can also live in YAML files
(`job-finder profile load`, `profile export`); see
[`profiles/example.yaml`](profiles/example.yaml).

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

## The site

Each profile signs in with its own name and password and sees only its own
data. There is no sign-up: `profile create` makes the profile and its first password
from the terminal (`profile password` resets one); after that, each person can change
their own under **Account** (the other devices are signed out).

```bash
docker compose exec job-finder job-finder profile create <name>
# or, without an interactive terminal:
printf '%s\n' "$PASSWORD" | docker compose exec -T job-finder job-finder profile create <name> --stdin
```

Pages:

- **Jobs**: New / Saved / Applied / Dismissed tabs, minimum fit and work mode
  filters, sorted by fit or by posting date (either way); save, dismiss, restore.
  **I applied** moves a saved job to Applied, off the saved list; for learning it
  still counts as a job you liked. Dismissed shows the last 7 days
  (`JOBFINDER_WEB_DISMISSED_DAYS`); older dismissals leave the site but keep teaching
  Claude.
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
- **Account**: password, and linking Telegram: **Link Telegram** makes a one-time code
  (15 minutes) and a t.me link that sends it to the bot as `/start <code>`. **Unlink**
  stops the digest.
- **Status**: what a running search is doing (the page refreshes itself), how
  the last one went, each source's requests today and this month against its
  limits, recent runs with their warnings, and Claude's spend this month
  (total, per use, and yours).

The pages are plain HTML forms; no JavaScript.

### Reaching the site

The container publishes the site on `127.0.0.1:8000` only, so by default it is
reachable from the machine itself. Two ways to open it to other devices:

- **Behind a reverse proxy with HTTPS** (recommended, and the only option for access
  over the internet): any proxy that terminates TLS and forwards to
  `http://127.0.0.1:8000`. It should keep the `Host` header (or send
  `X-Forwarded-Host`) and send `X-Forwarded-Proto: https`; the session cookie is then
  marked `Secure`.
- **On a trusted local network, over plain HTTP**: set `BIND_ADDRESS=0.0.0.0` in `.env`
  and run `docker compose up -d`; the site is at `http://<machine-ip>:8000`. The
  password and the session travel unencrypted, so never forward that port on a router.
  Docker publishes ports past host firewalls such as `ufw`.

POSTs from other sites are refused (`Sec-Fetch-Site`, or `Origin` against the host),
and sign-in attempts are throttled.

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

## License

[MIT](LICENSE)
