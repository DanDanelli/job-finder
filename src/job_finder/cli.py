"""Command line: `job-finder ...`."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine

from job_finder.config import get_settings
from job_finder.db import repo
from job_finder.db import session as db_session
from job_finder.db.models import ProfileDB
from job_finder.domain import Job

if TYPE_CHECKING:
    from job_finder.llm.model_eval import EvalReport

app = typer.Typer(help="Job Finder: finds jobs and ranks them by fit with your CV.")
db_app = typer.Typer(help="Database.")
profile_app = typer.Typer(help="Search profiles.")
app.add_typer(db_app, name="db")
app.add_typer(profile_app, name="profile")
console = Console()

ProfileOption = Annotated[str, typer.Option("--profile", "-p", help="Profile name.")]
ProfilesOption = Annotated[
    list[str] | None, typer.Option("--profile", "-p", help="Only these profiles.")
]


@app.callback()
def _configure() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and the Telegram API carries the bot token in
    # the URL. These loggers stay at WARNING so the token never reaches the log.
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def _claude_errors() -> Iterator[None]:
    """A Claude API error becomes a short message instead of a traceback."""
    import anthropic

    from job_finder.llm.client import CostCapReached, InvalidResponse

    try:
        yield
    except anthropic.APIStatusError as e:
        message = e.body.get("error", {}).get("message") if isinstance(e.body, dict) else None
        console.print(f"[red]Claude API ({e.status_code}): {message or e}[/]")
        raise typer.Exit(1) from e
    except (anthropic.APIConnectionError, CostCapReached, InvalidResponse) as e:
        console.print(f"[red]Claude: {e}[/]")
        raise typer.Exit(1) from e


def _engine() -> Engine:
    return db_session.make_engine(get_settings().resolved_database_url)


def _profile_id(engine: Engine, name: str) -> int:
    with db_session.session(engine, commit=False) as s:
        db = repo.find_profile(s, name)
        if db is None:
            raise typer.BadParameter(f"profile '{name}' not found")
        return db.id


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target Alembic revision.")] = "head",
) -> None:
    """Creates or updates the database schema."""
    url = get_settings().resolved_database_url
    db_session.upgrade(url, revision)
    typer.echo(f"Database upgraded ({revision}): {url}")


@db_app.command("backup")
def db_backup() -> None:
    """Copies the database now (the app also does it daily) and prunes old copies."""
    from job_finder.db.backup import BackupError, backup, sqlite_path

    settings = get_settings()
    database = sqlite_path(settings.resolved_database_url)
    if database is None:
        raise typer.BadParameter("backups cover SQLite databases only")
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    try:
        result = backup(database, settings.resolved_backup_dir, today, settings.backup_keep_days)
    except BackupError as e:
        console.print(f"[red]Backup failed:[/] {e}")
        raise typer.Exit(1) from None
    typer.echo(f"Backup: {result.path} ({result.size // 1024} KB)")
    for path in result.removed:
        typer.echo(f"Removed: {path.name}")


@profile_app.command("load")
def profile_load(
    files: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Creates or updates profiles from YAML files."""
    from job_finder.profiles import read_profile_yaml

    with db_session.session(_engine()) as s:
        for file in files:
            profile = read_profile_yaml(file)
            existing = repo.find_profile(s, profile.name)
            if "data" not in profile.model_fields_set and existing is not None:
                # YAML without `data`: keep what was extracted from the CV.
                profile.data = repo.profile_from(existing).data
            repo.save_profile(s, profile)
            typer.echo(f"Profile '{profile.name}' saved ({len(profile.queries)} queries).")


@profile_app.command("import")
def profile_import(
    resume: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="PDF.")],
    profile: ProfileOption,
) -> None:
    """Reads the CV with Claude and fills in the profile's data and queries."""
    import shutil

    from job_finder.domain import Profile, Query
    from job_finder.llm.client import make_client
    from job_finder.llm.profile_extractor import extract_profile

    settings = get_settings()
    engine = _engine()
    target = settings.data_dir / "resumes" / f"{profile}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    if resume.resolve() != target.resolve():
        shutil.copyfile(resume, target)

    with db_session.session(engine) as s:
        db = repo.find_profile(s, profile) or repo.save_profile(s, Profile(name=profile))
        profile_id = db.id

    client = make_client(engine)
    with _claude_errors(), console.status(f"Reading the CV with {settings.profile_model}..."):
        extraction = extract_profile(
            client,
            target,
            model=settings.profile_model,
            language=settings.language,
            profile_id=profile_id,
        )

    with db_session.session(engine) as s:
        db = s.get_one(ProfileDB, profile_id)
        current = repo.profile_from(db)
        current.data = extraction.data
        # New terms are active only if the profile had no active query yet; otherwise they
        # come in disabled, for the user to review.
        had_active = any(q.active for q in current.queries)
        known = {q.term.casefold() for q in current.queries}
        new_terms = [term for term in extraction.search_terms if term.casefold() not in known]
        current.queries += [Query(term=term, active=not had_active) for term in new_terms]
        repo.save_profile(s, current)
        db.resume_path = str(target)

    d = extraction.data
    console.print(f"[bold]{profile}[/]: {d.seniority or '?'}, {d.years_experience or '?'} years")
    console.print(d.summary)
    console.print("[bold]Roles:[/] " + ", ".join(d.roles))
    console.print("[bold]Skills:[/] " + ", ".join(f"{k.name} ({k.weight})" for k in d.skills))
    console.print("[bold]Languages:[/] " + ", ".join(f"{x.code} {x.level}" for x in d.languages))
    console.print(
        "[bold]New queries:[/] "
        + (", ".join(new_terms) or "none")
        + ("" if not had_active or not new_terms else " [yellow](disabled, review them)[/]")
    )
    console.print(
        f"Claude spend this month: US$ {client.spent_this_month():.4f}. "
        f"Review with `job-finder profile export {profile} > profile.yaml`, edit, then "
        "`job-finder profile load profile.yaml`."
    )


@profile_app.command("export")
def profile_export(name: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Prints the profile as YAML, in the format `profile load` reads."""
    from job_finder.profiles import write_profile_yaml

    with db_session.session(_engine(), commit=False) as s:
        db = repo.find_profile(s, name)
        if db is None:
            raise typer.BadParameter(f"profile '{name}' not found")
        typer.echo(write_profile_yaml(repo.profile_from(db)), nl=False)


def _clean_password(raw: str) -> str:
    """Drops what terminals add around typed or pasted text: bracketed-paste markers
    and line endings (a TTY chain such as ssh -t + docker exec may pass "\\r")."""
    for marker in ("\x1b[200~", "\x1b[201~"):
        raw = raw.replace(marker, "")
    return raw.strip("\r\n")


@profile_app.command("password")
def profile_password(
    name: Annotated[str, typer.Argument(help="Profile name (also the site username).")],
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the password from the first line of stdin.")
    ] = False,
) -> None:
    """Sets the profile's site password (asked twice, never echoed). Signs out its
    open sessions."""
    import sys

    from job_finder.web import auth

    engine = _engine()
    profile_id = _profile_id(engine, name)
    if stdin:
        password = _clean_password(sys.stdin.readline())
    else:
        password = _clean_password(typer.prompt("New password", hide_input=True))
        again = _clean_password(typer.prompt("Repeat it", hide_input=True))
        if again != password:
            raise typer.BadParameter(
                f"the two entries differ ({len(password)} and {len(again)} characters)"
            )
    try:
        hashed = auth.hash_password(password)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    with db_session.session(engine) as s:
        s.get_one(ProfileDB, profile_id).password_hash = hashed
        auth.end_all_sessions(s, profile_id)
    typer.echo(f"Password set for '{name}'. The site username is '{name}'.")


@profile_app.command("list")
def profile_list() -> None:
    """Lists the profiles."""
    with db_session.session(_engine()) as s:
        for p in repo.list_profiles(s):
            profile = repo.profile_from(p)
            terms = ", ".join(q.term for q in profile.queries if q.active) or "-"
            typer.echo(f"{profile.name}: {terms}")


@app.command()
def run(
    profile: ProfilesOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Writes nothing.")] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skips Claude scoring; matches stay pending.")
    ] = False,
) -> None:
    """Runs once: collects, normalizes, deduplicates, filters and scores per profile."""
    from job_finder.service import NoProfiles, full_run

    settings = get_settings()
    engine = _engine()
    try:
        with _claude_errors(), console.status("Running..."):
            report = full_run(
                engine,
                settings,
                names=profile or None,
                kind="on_demand",
                dry_run=dry_run,
                score=not no_llm,
            )
    except NoProfiles as e:
        raise typer.BadParameter(str(e)) from e

    summary = report.run
    sources = ", ".join(f"{f}: {n}" for f, n in summary.by_source.items()) or "none"
    console.print(
        f"[bold]Collected:[/] {summary.collected} ({sources}) · [bold]new:[/] {summary.new}"
        + (" · [yellow]dry run, nothing written[/]" if dry_run else "")
    )
    for pr in report.profiles:
        f = pr.filters
        discards = ", ".join(f"{m}: {n}" for m, n in f.discards.most_common()) or "none"
        console.print(
            f"[bold]{pr.name}[/]: {len(f.approved)} of {f.evaluated} passed the filters"
            f" (discards: {discards})"
        )
        if pr.scoring:
            sc = pr.scoring
            console.print(
                f"  {sc.scored} jobs scored of {sc.pending} pending ({sc.skipped} off-profile)"
            )
            _show_ranking(engine, pr.profile_id)
        elif dry_run:
            _show_approved(f.approved)
    for warning in {w for pr in report.profiles for w in report.warnings_for(pr.profile_id)}:
        console.print(f"[red]⚠ {warning}[/]")


def _show_approved(jobs: list[Job]) -> None:
    table = Table()
    for column in ("Job", "Company", "Location", "Salary", "Link"):
        table.add_column(column, overflow="fold")
    for j in jobs:
        sal = j.salary
        salary = (
            f"{sal.currency or ''} {sal.minimum:,.0f}-{sal.maximum:,.0f}/{sal.period or '?'}"
            if sal and sal.minimum is not None and sal.maximum is not None
            else "-"
        )
        table.add_row(j.title, j.company or "-", j.location_text or "-", salary, str(j.url))
    console.print(table)


def _score(engine: Engine, names: list[str] | None, run_id: int | None = None) -> None:
    from job_finder.llm.client import make_client
    from job_finder.llm.scorer import score_pending

    settings = get_settings()
    client = make_client(engine)
    with db_session.session(engine, commit=False) as s:
        profiles = [(p.id, p.name) for p in repo.list_profiles(s, names)]
    if not profiles:
        raise typer.BadParameter("no profile found; use `job-finder profile load`")

    for profile_id, name in profiles:
        with (
            _claude_errors(),
            console.status(f"Scoring {name}'s jobs with {settings.scoring_model}..."),
        ):
            r = score_pending(
                engine,
                client,
                profile_id,
                model=settings.scoring_model,
                language=settings.language,
                top_n=settings.prerank_top_n,
                cutoff=settings.relevance_cutoff,
                now=datetime.now(UTC),
                run_id=run_id,
            )
        console.print(
            f"[bold]{name}[/]: {r.scored} jobs scored of {r.pending} pending"
            f" ({r.skipped} off-profile)" + (f", {len(r.failures)} failures" if r.failures else "")
        )
        for failure in r.failures:
            console.print(f"[red]⚠ {failure}[/]")
        if r.interrupted:
            console.print(f"[red]⚠ {r.interrupted}[/]")
            break
        _show_ranking(engine, profile_id)
    console.print(f"Claude spend this month: US$ {client.spent_this_month():.4f}")


def _show_ranking(engine: Engine, profile_id: int, limit: int = 10) -> None:
    from sqlalchemy import select

    from job_finder.db.models import MatchDB
    from job_finder.domain import MatchStatus

    with db_session.session(engine, commit=False) as s:
        matches = s.scalars(
            select(MatchDB)
            .where(MatchDB.profile_id == profile_id, MatchDB.status == MatchStatus.NEW)
            .order_by(MatchDB.score.desc())
            .limit(limit)
        ).all()
        if not matches:
            return
        table = Table(title="Best new jobs")
        for column in ("Score", "Job", "Company", "Why", "Link"):
            table.add_column(column, overflow="fold")
        for m in matches:
            why = m.summary or ""
            if m.dealbreakers:
                why += " [red]✗ " + "; ".join(m.dealbreakers) + "[/]"
            table.add_row(str(m.score), m.job.title, m.job.company or "-", why, m.job.url)
        console.print(table)


@app.command()
def score(profile: ProfilesOption = None) -> None:
    """Scores pending jobs with Claude (without collecting new ones)."""
    if get_settings().anthropic_api_key is None:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set (put it in .env)")
    _score(_engine(), profile or None)


@app.command()
def digest(profile: ProfileOption) -> None:
    """Sends the digest of scored, not yet notified jobs through Telegram now."""
    import asyncio

    from job_finder.telegram.bot import TelegramSender, make_application
    from job_finder.telegram.digest import send_digest

    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise typer.BadParameter("JOBFINDER_TELEGRAM_BOT_TOKEN is not set (put it in .env)")
    engine = _engine()
    with db_session.session(engine, commit=False) as s:
        db = repo.find_profile(s, profile)
        if db is None:
            raise typer.BadParameter(f"profile '{profile}' not found")
        if db.telegram_chat_id is None:
            raise typer.BadParameter(
                f"profile '{profile}' has no telegram_chat_id; send /start to the bot to get it"
            )
        profile_id = db.id

    async def send() -> int:
        bot_app = make_application(settings.telegram_bot_token.get_secret_value())  # type: ignore[union-attr]
        async with bot_app.bot as bot:
            return await send_digest(
                engine,
                TelegramSender(bot),
                profile_id,
                min_score=settings.digest_min_score,
                maximum=settings.digest_max_jobs,
                language=settings.language,
            )

    console.print(f"{asyncio.run(send())} jobs sent to {profile}.")


@app.command()
def evaluate(
    entry: Annotated[str, typer.Argument(help="The job's link or its text.")],
    profile: ProfileOption,
) -> None:
    """Evaluates a single job (the same as sending the link to the bot)."""
    import re

    from job_finder.telegram import actions

    settings = get_settings()
    engine = _engine()
    text, buttons = actions.evaluate_in_chat(engine, settings, _profile_id(engine, profile), entry)
    console.print(re.sub(r"</?[a-z]+>", "", text), markup=False, highlight=False)
    for b in buttons:
        if b.url:
            console.print(b.url, markup=False)


DEFAULT_EVAL_MODELS = ["claude-haiku-5-5", "claude-opus-5-5"]


@app.command("eval")
def eval_models(
    profile: ProfileOption,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", "-m", help="Model to evaluate (repeatable)."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="A sample of N jobs, saved and dismissed alternating."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the cost confirmation.")] = False,
) -> None:
    """Scores the profile's saved and dismissed jobs again with each model and measures how
    well the scores separate them. Spends Claude credit; writes only to llm_costs."""
    from job_finder.llm.client import make_client
    from job_finder.llm.model_eval import estimate, load_labeled, run_eval, sample

    settings = get_settings()
    if settings.anthropic_api_key is None:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set (put it in .env)")
    models = model or DEFAULT_EVAL_MODELS
    engine = _engine()
    profile_id = _profile_id(engine, profile)
    labeled = sample(load_labeled(engine, profile_id), limit)
    jobs = len(labeled)
    saved = sum(item.saved for item in labeled)
    if saved == 0 or saved == jobs:
        raise typer.BadParameter(
            f"'{profile}' needs both saved and dismissed jobs ({saved} saved of {jobs})"
        )

    guess = estimate(engine, models, jobs)
    client = make_client(engine)
    spent = client.spent_this_month()
    basis = f"from {guess.basis} past scoring calls" if guess.basis else "a rough guess"
    console.print(
        f"[bold]{profile}[/]: {jobs} labeled jobs ({saved} saved) x {len(models)} models"
        f" ({', '.join(models)}) = {guess.calls} calls, about US$ {guess.cost_usd:.2f}"
        f" ({basis}). Spent this month: US$ {spent:.2f} of US$ {settings.monthly_cost_cap_usd:.2f}."
    )
    if not yes:
        typer.confirm("Run the eval?", abort=True)

    with _claude_errors(), console.status("Evaluating...") as status:
        report = run_eval(
            engine,
            client,
            profile_id,
            models=models,
            language=settings.language,
            limit=limit,
            progress=lambda m, i, n: status.update(f"Evaluating with {m}: {i}/{n}"),
        )
    _show_eval(report, settings.digest_min_score)
    console.print(f"Claude spend this month: US$ {client.spent_this_month():.4f}")


def _show_eval(report: "EvalReport", digest_min_score: int) -> None:
    from job_finder.llm.model_eval import STORED

    def num(value: float | None, fmt: str = ".2f") -> str:
        return "-" if value is None else format(value, fmt)

    def short(model: str) -> str:
        return model.removeprefix("claude-")

    columns = [STORED, *report.models]
    table = Table(title=f"Saved vs dismissed: {report.profile}")
    table.add_column("")
    for column in columns:
        table.add_column("production" if column == STORED else short(column), justify="right")
    stats = {c: report.metrics(c, digest_min_score) for c in columns}

    def cost(column: str, per_job: bool = False) -> str:
        spent = report.cost_by_model.get(column)
        calls = report.calls_by_model.get(column, 0)
        if spent is None or (per_job and not calls):
            return "-"
        return f"{spent / calls:.4f}" if per_job else f"{spent:.3f}"

    rows: list[tuple[str, Callable[[str], str]]] = [
        ("Jobs scored", lambda c: str(stats[c].n)),
        ("AUC (0.5 = chance)", lambda c: num(stats[c].auc)),
        ("Mean score, saved", lambda c: num(stats[c].mean_saved, ".1f")),
        ("Mean score, dismissed", lambda c: num(stats[c].mean_dismissed, ".1f")),
        ("Gap", lambda c: num(stats[c].mean_gap, "+.1f")),
        ("Top-k precision, k = saved", lambda c: num(stats[c].precision_at_saved)),
        (
            f"Saved at ≥{digest_min_score} (digest)",
            lambda c: f"{stats[c].saved_in_digest}/{stats[c].saved}",
        ),
        (
            f"Dismissed at ≥{digest_min_score} (digest)",
            lambda c: f"{stats[c].dismissed_in_digest}/{stats[c].n - stats[c].saved}",
        ),
        ("US$ (this eval)", cost),
        ("US$ per job", lambda c: cost(c, per_job=True)),
    ]
    for name, cell in rows:
        table.add_row(name, *(cell(c) for c in columns))
    console.print(table)

    jobs = Table(title="Per job")
    for header in (
        "Decision",
        "Job",
        "Company",
        *("stored" if c == STORED else short(c) for c in columns),
    ):
        jobs.add_column(header, overflow="fold")
    for row in sorted(report.rows, key=lambda r: (not r.labeled.saved, r.labeled.job.title)):
        item = row.labeled
        scores = [item.stored_score if c == STORED else row.scores.get(c) for c in columns]
        jobs.add_row(
            "saved" if item.saved else "dismissed",
            item.job.title,
            item.job.company or "-",
            *("-" if s is None else str(s) for s in scores),
        )
    console.print(jobs)
    for failure in report.failures:
        console.print(f"[red]⚠ {failure}[/]")
    if report.interrupted:
        console.print(f"[red]⚠ {report.interrupted}[/]")


@app.command()
def serve() -> None:
    """Starts the web application."""
    import uvicorn

    from job_finder.web.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()
