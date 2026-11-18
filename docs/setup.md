# Setting up Job Finder

From zero to a Telegram digest of jobs scored for you, step by step. Plan on about an
hour, most of it filling in your profile.

You end up with one Docker container that collects jobs twice a day, scores the most
promising ones with Claude, sends the best to Telegram and serves a small site where you
review them. One installation can serve several people, each with their own profile,
password and Telegram chat.

## 1. What you need

| | Required | Notes |
|---|---|---|
| A machine that stays on | yes | Anything that runs Docker: a Raspberry Pi 4 or 5 (64-bit OS), a home server, a small VPS. 1 GB of free RAM and a few GB of disk are plenty. |
| Docker with Compose | yes | `docker compose version` should work. |
| Anthropic API key | yes | Claude reads your CV and scores jobs. Pay as you go; see [Costs](#costs). |
| Telegram account | yes | For the digest and the bot. |
| JSearch key (Google for Jobs) | no | Adds jobs from Google for Jobs (LinkedIn, Indeed, company sites...). |
| Adzuna keys | no | Adds Adzuna's job search, strong in Brazil and ~18 other countries. |
| Tailscale | no | To reach the site from outside home, with HTTPS. |

Without the optional keys, jobs come from Remotive, Remote OK, Himalayas and We Work
Remotely, which are mostly remote jobs.

## 2. Get the code

```bash
git clone https://github.com/DanDanelli/job-finder.git
cd job-finder
mkdir -p data
```

The container runs as user id 1000 and writes to `./data`. Create the folder yourself as
above (if Docker creates it, it belongs to root and the app cannot write to it). If your
user is not uid 1000 (`id -u`), run `sudo chown 1000:1000 data`.

## 3. Get the keys

**Anthropic.** At [console.anthropic.com](https://console.anthropic.com), add some credit,
create an API key, and set a monthly spend limit for the workspace as a second safety net
(the app has its own cap too).

**Telegram bot.** In Telegram, talk to [@BotFather](https://t.me/BotFather), send
`/newbot`, pick a name and a username. It answers with a token like `123456:ABC...`.
One bot serves everyone in your installation.

**JSearch (optional).** Create an account at [app.openwebninja.com](https://app.openwebninja.com)
and copy the JSearch API key. The app spreads its searches to fit a monthly quota
(`JOBFINDER_JSEARCH_MONTHLY_LIMIT`, 190 by default); set it to your plan's limit.

**Adzuna (optional).** Register at [developer.adzuna.com](https://developer.adzuna.com) and
copy the app id and app key. Their terms allow personal job search; commercial use needs a
licence.

## 4. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Open `.env` and set at least:

```bash
ANTHROPIC_API_KEY=sk-ant-...
JOBFINDER_TELEGRAM_BOT_TOKEN=123456:ABC...
JOBFINDER_TIMEZONE=America/Sao_Paulo        # yours, for the run times
JOBFINDER_LANGUAGE=en                       # or pt-BR: bot messages and Claude's text
```

Optional, but worth a look:

```bash
JOBFINDER_RUN_TIMES=["08:00","18:00"]       # when the scheduled runs happen
JOBFINDER_DIGEST_MIN_SCORE=70               # minimum fit to reach Telegram
JOBFINDER_MONTHLY_COST_CAP_USD=5            # Claude stops scoring at this spend

# Optional sources: add them to the list and set their keys
JOBFINDER_CONNECTORS='["remotive","remoteok","himalayas","jsearch","adzuna","weworkremotely"]'
JOBFINDER_JSEARCH_API_KEY=...
JOBFINDER_ADZUNA_APP_ID=...
JOBFINDER_ADZUNA_APP_KEY=...
JOBFINDER_ADZUNA_COUNTRIES='["br"]'
```

Every variable is described in [`.env.example`](../.env.example). Never commit `.env`.

## 5. Start it

```bash
docker compose up -d --build
docker compose ps        # wait for "(healthy)"
docker compose logs -f   # Ctrl+C to stop following
```

The first build takes a few minutes on a Raspberry Pi. The database is created on start,
in `data/job_finder.db`.

Only one copy of the app may run with a given bot token: Telegram delivers each message to
a single listener. Don't run `job-finder serve` on your laptop with the same token while
the server is up.

## 6. Link your Telegram chat

Open your bot in Telegram and send `/start`. Since no profile knows your chat yet, it
replies with your **chat id**, a number. Keep it for the next step.

Everyone who will use this installation does the same with the same bot.

## 7. Create your profile

A profile says what to search, what to rule out, and who you are. Start from the example:

```bash
mkdir -p data/profiles
cp profiles/example.yaml data/profiles/me.yaml
```

Edit `data/profiles/me.yaml`:

- `name`: a short login name, e.g. `alex`.
- `telegram_chat_id`: the number from step 6.
- `queries`: the job titles to search for, 4 to 8 of them, in the languages jobs are
  posted in (e.g. both "Cloud Architect" and "Arquiteto de Cloud" in Brazil). `countries`
  limits a query to some countries; empty means worldwide.
- `filters`: the hard rules, applied before Claude sees anything:
  - `countries`: where you accept onsite or hybrid jobs;
  - `remote_from`: the country you live in, so remote jobs must accept people from there;
  - `work_modes`, `contract_types`;
  - `min_salary`, or `min_salary_by_contract` for a different minimum per contract type;
  - `blocked_companies`, `blocked_keywords`, `max_age_days`.
- `preferences`: a few sentences in plain words for Claude ("I prefer product companies
  over consultancies").
- `data`: leave the example's or delete the block. Step 9 fills it from your CV.

Load it:

```bash
docker compose exec job-finder job-finder profile load /data/profiles/me.yaml
```

Send `/start` to the bot again: it now greets you by name and lists the commands.

Later changes are easier on the site (**Profile** page). Loading the YAML again overwrites
searches and filters, but keeps what was read from the CV if the file has no `data`.

## 8. Set your password and open the site

```bash
docker compose exec job-finder job-finder profile password alex
```

The site is at `http://localhost:8000` on the machine itself. From other devices, pick
one of the two options below. Sign in with the profile name and that password.

**On your home network.** Add `BIND_ADDRESS=0.0.0.0` to `.env` and run
`docker compose up -d`. The site is then at `http://<machine-ip>:8000`. This is plain HTTP:
use it only on a network you trust and never forward the port on your router. Docker
publishes the port past `ufw`, so the firewall will not filter it.

**From anywhere, with Tailscale.** Install [Tailscale](https://tailscale.com) on the machine
and on your phone or laptop, then on the machine:

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8000
```

The site is at `https://<machine>.<tailnet>.ts.net:8443` (the full name is in the Tailscale
admin console). To give someone else access without adding them to your tailnet, share just
that machine with them (Machines → ⋯ → Share). Put the address in `.env` as
`JOBFINDER_SITE_URL` so Telegram messages can link to the site.

## 9. Read your CV

On the site, go to **CV** and upload a PDF. Claude reads it in the background (a minute or
so) and shows what it extracted: summary, roles, seniority, skills, languages, industries,
certifications and suggested searches. Fix anything wrong, then save. Nothing changes in
your profile until you do.

The same form edits that data at any time. From the terminal, the equivalent is to put
the PDF in `data/` and run
`docker compose exec job-finder job-finder profile import /data/cv.pdf -p alex`.

## 10. First search

On the **Jobs** page, click **Search now**. **Status** shows what it is doing. The first run
takes a few minutes: it reads every source and scores the top 30 jobs for you.

Jobs at or above `JOBFINDER_DIGEST_MIN_SCORE` go to Telegram at the next scheduled time.
To get the digest right away:

```bash
docker compose exec job-finder job-finder digest -p alex
```

From then on, it runs by itself at `JOBFINDER_RUN_TIMES`.

## 11. Teach it your taste

This part makes the digest get better over time:

- 👍 or 👎 every job you look at, on Telegram or on the site.
- Say **why** in a line: reply to the job's message on Telegram, or type it in the note
  field on the site. "Consultancy placing people at a client" teaches more than 👎 alone.
- **I applied** on a saved job moves it to the Applied tab. It still counts as a job you
  liked.
- Once a week, after a few decisions, Claude proposes rules from them. You get a message
  on Telegram; review them under **Profile → Learned from your feedback**. They only count
  once you approve them.
- `/evaluate <link>` on Telegram scores any job you found elsewhere.

## 12. Keep it running

**Backups.** The app copies the database every day at 03:30 and keeps 7 days. By default
the copies land in `data/backup`, on the same disk. Point them to another disk with
`BACKUP_HOST_DIR=/path/on/another/disk` in `.env`, then `docker compose up -d`. To restore,
stop the container and copy a `job_finder-<date>.db` over `data/job_finder.db`.

**Updates.**

```bash
git pull
docker compose up -d --build
```

Database migrations run on start. Avoid updating during a scheduled run: the **Status**
page shows when one is going.

**Logs.** `docker compose logs --since 1h`. Each run's warnings (a source down, a quota
reached) also show on **Status** and at the end of the digest.

## Costs

Claude is the only part that costs money. What one installation with two people spent in
its first days, with the default settings:

| What | Cost |
|---|---|
| Scoring one job (Claude Haiku, scheduled runs batched at half price) | about US$ 0.0002-0.0004 |
| Reading one CV (Claude Opus) | about US$ 0.07 |
| One weekly rules proposal (Claude Opus) | about US$ 0.07 |

Each job is scored once per person, at most 30 per person per run, and only after passing
the filters and the relevance cut. The monthly cap (`JOBFINDER_MONTHLY_COST_CAP_USD`,
US$ 5 by default) is checked before every call: when it is reached, scoring stops until
next month and the digest says so. `job-finder eval` and **Suggest rules now** show an
estimate and ask before spending.

The job sources are free within their limits, and the app keeps to them: it counts requests
per day and month for each source (see **Status**).

## Troubleshooting

| Symptom | What to check |
|---|---|
| Container restarts, "permission denied" on `/data` | `./data` belongs to root: `sudo chown -R 1000:1000 data`. |
| The bot does not answer at all | Token in `.env`; `docker compose logs` for Telegram errors; no other copy of the app polling with the same token. |
| `/start` keeps returning the chat id | `telegram_chat_id` in your YAML is wrong or the profile was not loaded (step 7). |
| No jobs, or very few | **Status** for source errors; queries too narrow or filters too strict; `docker compose exec job-finder job-finder run --dry-run -p alex` shows what passes the filters without writing anything (it does query the sources, so it counts toward their daily limits). |
| Jobs found but nothing on Telegram | Their scores are below `JOBFINDER_DIGEST_MIN_SCORE`: they are still on the site. |
| "Monthly cap reached" | Raise `JOBFINDER_MONTHLY_COST_CAP_USD`, or wait for next month. |
| A job link opens a "you have been blocked" page | The job site's own firewall. For Google for Jobs results, the "via ... (Google for Jobs)" link on the card lists other sites carrying the same job. |
