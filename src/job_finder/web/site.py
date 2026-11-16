"""The site: sign-in, jobs, profile, CV, status and account pages (Jinja templates, plain
forms; pages that wait on background work refresh themselves, with no JavaScript).

Protection:
- every page except /login needs a session; a profile only ever sees its own data;
- the session cookie is HttpOnly, SameSite=Lax and (by default) Secure;
- POSTs from another origin are refused (CSRF), on top of SameSite.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from job_finder.config import Settings
from job_finder.db.models import ProfileDB
from job_finder.db.session import session
from job_finder.domain import ContractType, WorkMode
from job_finder.i18n import t
from job_finder.llm.profile_extractor import ProfileExtraction
from job_finder.scheduler import Scheduler, next_run
from job_finder.telegram.digest import PERIODS, SOURCE_NAMES, WORK_MODES, short_places
from job_finder.web import auth, cv, data, forms, status

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
COOKIE = "jf_session"
SCORE_STEPS = (0, 50, 60, 70, 80)


class LoginRequired(Exception):
    pass


def _same_origin(request: Request) -> bool:
    """Modern browsers say where a request comes from in Sec-Fetch-Site; that is the
    first check. Otherwise the Origin header must match the host (directly or as the
    proxy forwarded it). "Origin: null" (opaque origins, sandboxed frames) is refused."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser form (or a very old browser): SameSite still applies
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    return urlsplit(origin).netloc in hosts - {None}


class OriginCheck(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and not _same_origin(request):
            log.warning(
                "refused POST %s: sec-fetch-site=%s origin=%s host=%s forwarded-host=%s",
                request.url.path,
                request.headers.get("sec-fetch-site"),
                request.headers.get("origin"),
                request.headers.get("host"),
                request.headers.get("x-forwarded-host"),
            )
            return Response("cross-origin request refused", status_code=403)
        return await call_next(request)


def _claude_extractor(engine: Engine, settings: Settings) -> cv.Extractor:
    def extract(pdf: Path, profile_id: int) -> ProfileExtraction:
        from job_finder.llm.client import make_client
        from job_finder.llm.profile_extractor import extract_profile

        return extract_profile(
            make_client(engine),
            pdf,
            model=settings.profile_model,
            language=settings.language,
            profile_id=profile_id,
        )

    return extract


def mount_site(
    app: FastAPI,
    engine: Engine,
    settings: Settings,
    scheduler: Scheduler,
    *,
    extract: cv.Extractor | None = None,
) -> None:
    lang = settings.language
    templates = Jinja2Templates(directory=HERE / "templates")
    throttle = auth.LoginThrottle()
    router = APIRouter()
    store = cv.CVStore(settings.data_dir)
    extractions = cv.Extractions(store, extract or _claude_extractor(engine, settings))

    def tr(key: str, **params: Any) -> str:
        return t(lang, key, **params)

    def salary(item: Any) -> str | None:
        if item.salary_range is None:
            return None
        if item.salary_period in PERIODS:
            return f"{item.salary_range}/{tr(f'period.{item.salary_period}')}"
        return str(item.salary_range)

    def work_mode(mode: str) -> str | None:
        return tr(f"work_mode.{mode}") if mode in WORK_MODES else None

    def level_label(code: str) -> str:
        return tr(f"level.{code}")

    level_codes = {level_label(c).lower(): c for c in cv.LEVELS}

    def progress_text(key: str, params: dict[str, Any]) -> str:
        if "source" in params:
            params = params | {"source": SOURCE_NAMES.get(params["source"], params["source"])}
        return tr(key, **params)

    def when(moment: datetime | None) -> str:
        if moment is None:
            return ""
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(settings.timezone)).strftime("%d/%m %H:%M")

    templates.env.globals.update(
        t=tr,
        lang=lang,
        salary=salary,
        work_mode=work_mode,
        short_places=short_places,
        when=when,
        progress_text=progress_text,
        level_label=level_label,
        SENIORITIES=cv.SENIORITIES,
        LEVELS=cv.LEVELS,
        WORK_MODES=[m.value for m in WorkMode if m != WorkMode.UNKNOWN],
        CONTRACT_TYPES=[c.value for c in ContractType],
        SALARY_CONTRACTS=[c.value for c in forms.SALARY_CONTRACTS],
        PERIODS=forms.PERIODS,
    )

    def now() -> datetime:
        return datetime.now(UTC)

    def current_profile(request: Request) -> ProfileDB:
        with session(engine, commit=False) as s:
            profile = auth.profile_for_token(s, request.cookies.get(COOKIE), now())
            if profile is None:
                raise LoginRequired
            s.expunge(profile)
            return profile

    Profile = Annotated[ProfileDB, Depends(current_profile)]

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("running", scheduler.current)
        context.setdefault("busy", scheduler.busy)
        return templates.TemplateResponse(request, name, context)

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html", error=None, username="")

    @router.post("/login")
    def login(
        request: Request,
        username: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
    ) -> Response:
        name = username.strip().lower()
        if throttle.blocked(name):
            return page(request, "login.html", error=tr("web.login.blocked"), username=name)
        with session(engine) as s:
            profile = s.scalar(select(ProfileDB).where(func.lower(ProfileDB.name) == name))
            stored = profile.password_hash if profile else None
            if profile is None or not auth.verify_password(password, stored):
                if profile is None:
                    auth.verify_password(password, None)  # same cost as a real check
                throttle.failed(name)
                failed = page(request, "login.html", error=tr("web.login.failed"), username=name)
                failed.status_code = 401
                return failed
            throttle.succeeded(name)
            token = auth.start_session(s, profile.id, now(), settings.web_session_days)
        response = RedirectResponse("/jobs", status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.web_session_days * 86400,
            httponly=True,
            secure=settings.web_secure_cookie,
            samesite="lax",
        )
        return response

    @router.post("/logout")
    def logout(request: Request) -> Response:
        with session(engine) as s:
            auth.end_session(s, request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @router.get("/")
    def home(profile: Profile) -> Response:
        return RedirectResponse("/jobs", status_code=303)

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs(
        request: Request,
        profile: Profile,
        tab: str = "new",
        min_score: int = 0,
        mode: str = "",
        sort: str = "score_desc",
    ) -> HTMLResponse:
        tab = tab if tab in data.TABS else "new"
        sort = sort if sort in data.SORTS else "score_desc"
        cards = data.job_cards(
            engine, profile.id, tab, min_score=min_score, work_mode=mode or None, sort=sort
        )
        return page(
            request,
            "jobs.html",
            profile=profile,
            nav="jobs",
            tab=tab,
            tabs=list(data.TABS),
            counts=data.counts(engine, profile.id),
            cards=cards,
            min_score=min_score,
            mode=mode,
            sort=sort,
            sorts=data.SORTS,
            score_steps=SCORE_STEPS,
            query=urlencode({"tab": tab, "min_score": min_score, "mode": mode, "sort": sort}),
        )

    @router.post("/matches/{match_id}")
    def change_status(
        profile: Profile,
        match_id: int,
        action: Annotated[str, Form()],
        back: Annotated[str, Form()] = "",
    ) -> Response:
        if not data.set_status(engine, profile.id, match_id, action):
            return Response(status_code=404)
        # Only a query string of our own: never redirect elsewhere.
        target = "/jobs?" + back if back and "://" not in back and "\\" not in back else "/jobs"
        return RedirectResponse(target, status_code=303)

    def profile_page(
        request: Request, profile: ProfileDB, values: dict[str, str], **extra: Any
    ) -> HTMLResponse:
        return page(request, "profile.html", profile=profile, nav="profile", v=values, **extra)

    def values_from(form: data.ProfileForm) -> dict[str, str]:
        f = form.filters
        v = {
            "queries": forms.queries_text(form.queries),
            "countries": forms.countries_text(f.countries),
            "cities": "\n".join(f.cities),
            "remote_from": forms.countries_text(f.remote_from),
            "blocked_companies": "\n".join(f.blocked_companies),
            "blocked_keywords": "\n".join(f.blocked_keywords),
            "max_age_days": str(f.max_age_days),
            "preferences": form.preferences,
            "paused": "on" if form.paused else "",
        }
        salaries = {"min_any": f.min_salary} | {
            f"min_{c.value}": f.min_salary_by_contract.get(c) for c in forms.SALARY_CONTRACTS
        }
        for prefix, s in salaries.items():
            v[f"{prefix}_amount"] = forms.amount_text(s.minimum if s else None)
            v[f"{prefix}_currency"] = (s.currency if s else None) or "BRL"
            v[f"{prefix}_period"] = (s.period if s else None) or "month"
        v["work_modes"] = ",".join(m.value for m in f.work_modes)
        v["contract_types"] = ",".join(c.value for c in f.contract_types)
        return v

    @router.get("/profile", response_class=HTMLResponse)
    def profile_view(
        request: Request, profile: Profile, dropped: int | None = None
    ) -> HTMLResponse:
        form = data.load_profile(engine, profile.id)
        if dropped is None:
            notice = None
        elif dropped:
            notice = tr("web.profile.saved_dropped", n=dropped)
        else:
            notice = tr("web.profile.saved")
        return profile_page(request, profile, values_from(form), notice=notice, errors={})

    @router.post("/profile", response_class=HTMLResponse)
    async def profile_save(request: Request, profile: Profile) -> Response:
        raw = await request.form()
        single = {k: str(v) for k, v in raw.items() if isinstance(v, str)}
        checked = {k: [str(x) for x in raw.getlist(k)] for k in ("work_modes", "contract_types")}
        current = data.load_profile(engine, profile.id)
        parsed = forms.parse_profile(single, checked, current.filters)
        if parsed.errors:
            values = single | {k: ",".join(v) for k, v in checked.items()}
            response = profile_page(
                request, profile, values, notice=None, errors=parsed.errors.fields
            )
            response.status_code = 422
            return response
        dropped = data.save_profile(
            engine,
            profile.id,
            queries=parsed.queries,
            filters=parsed.filters,
            preferences=parsed.preferences,
            paused=parsed.paused,
            now=now(),
        )
        return RedirectResponse(f"/profile?dropped={dropped}", status_code=303)

    # --- CV --------------------------------------------------------------------------

    def cv_page(
        request: Request,
        profile: ProfileDB,
        values: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        notice: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        job = extractions.get(profile.id)
        # Decided once: the draft is written before the task ends, so "not running"
        # here means the draft (if any) is already on disk.
        extracting = job is not None and job.running
        draft = None if extracting else store.draft(profile.id)
        current, queries = data.profile_data(engine, profile.id)
        if values is None:
            values = cv.data_values(draft.data if draft else current, level_label)
        response = page(
            request,
            "cv.html",
            profile=profile,
            nav="cv",
            v=values,
            errors=errors or {},
            notice=notice,
            extracting=extracting,
            extract_error=job.error if job else None,
            draft=draft,
            terms=data.new_terms(draft.search_terms, queries) if draft else [],
            has_resume=bool(profile.resume_path),
            max_mb=cv.MAX_PDF_BYTES // (1024 * 1024),
        )
        response.status_code = status_code
        return response

    @router.get("/cv", response_class=HTMLResponse)
    def cv_view(request: Request, profile: Profile, saved: int | None = None) -> HTMLResponse:
        notice = None
        if saved is not None:
            notice = tr("web.cv.saved_terms", n=saved) if saved else tr("web.cv.saved")
        return cv_page(request, profile, notice=notice)

    @router.post("/cv/upload", response_class=HTMLResponse)
    async def cv_upload(
        request: Request,
        profile: Profile,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> Response:
        job = extractions.get(profile.id)
        if job is not None and job.running:
            return RedirectResponse("/cv", status_code=303)
        content = await file.read(cv.MAX_PDF_BYTES + 1) if file is not None else b""
        error = None
        if not content:
            error = tr("web.cv.no_file")
        elif len(content) > cv.MAX_PDF_BYTES:
            error = tr("web.cv.too_big", mb=cv.MAX_PDF_BYTES // (1024 * 1024))
        elif not cv.is_pdf(content):
            error = tr("web.cv.not_pdf")
        elif settings.anthropic_api_key is None and extract is None:
            error = tr("warn.no_key")
        if error:
            return cv_page(request, profile, errors={"file": error}, status_code=422)
        extractions.clear(profile.id)
        store.save_upload(profile.id, content)
        extractions.start(profile.id)
        return RedirectResponse("/cv", status_code=303)

    @router.post("/cv", response_class=HTMLResponse)
    async def cv_save(request: Request, profile: Profile) -> Response:
        raw = await request.form()
        single = {k: str(v) for k, v in raw.items() if isinstance(v, str)}
        parsed = cv.parse_data(single, level_codes)
        if parsed.errors:
            return cv_page(request, profile, single, parsed.errors, status_code=422)
        job = extractions.get(profile.id)
        draft = None if job and job.running else store.draft(profile.id)
        resume_path = None
        terms: list[str] = []
        if draft is not None and single.get("from_draft"):
            suggested = set(draft.search_terms)  # only terms Claude suggested
            terms = [str(x) for x in raw.getlist("terms") if str(x) in suggested]
            resume_path = str(store.promote(profile.id))
        added = data.save_profile_data(
            engine, profile.id, parsed.data, add_terms=terms, resume_path=resume_path
        )
        extractions.clear(profile.id)
        return RedirectResponse(f"/cv?saved={added}", status_code=303)

    @router.post("/cv/discard")
    def cv_discard(profile: Profile) -> Response:
        job = extractions.get(profile.id)
        if job is None or not job.running:
            store.discard(profile.id)
            extractions.clear(profile.id)
        return RedirectResponse("/cv", status_code=303)

    # --- search and status -------------------------------------------------------------

    @router.post("/search")
    async def search_now(profile: Profile) -> Response:
        scheduler.start([profile.name], kind="on_demand")
        return RedirectResponse("/status", status_code=303)

    @router.get("/status", response_class=HTMLResponse)
    def status_view(request: Request, profile: Profile) -> HTMLResponse:
        # `current` before `last`: the scheduler records the outcome before clearing
        # `current`, so reading in this order always sees one of them.
        running = scheduler.current
        outcome = scheduler.last.get(profile.name)
        moment = now()
        upcoming = None
        if settings.schedule_enabled and settings.run_times:
            from zoneinfo import ZoneInfo

            upcoming = next_run(moment, settings.run_times, ZoneInfo(settings.timezone))
        return page(
            request,
            "status.html",
            profile=profile,
            nav="status",
            running=running,
            busy=running is not None or scheduler.busy,
            outcome=outcome,
            upcoming=upcoming,
            sources=status.sources(engine, settings, moment.date()),
            runs=status.recent_runs(engine),
            spend=status.spend(engine, settings, profile.id, moment),
            counts=data.counts(engine, profile.id),
            paused=data.load_profile(engine, profile.id).paused,
        )

    # --- account -----------------------------------------------------------------------

    @router.get("/account", response_class=HTMLResponse)
    def account_view(request: Request, profile: Profile, changed: int = 0) -> HTMLResponse:
        notice = tr("web.account.changed") if changed else None
        return page(
            request, "account.html", profile=profile, nav="account", notice=notice, error=None
        )

    @router.post("/account", response_class=HTMLResponse)
    def account_save(
        request: Request,
        profile: Profile,
        current: Annotated[str, Form()] = "",
        new: Annotated[str, Form()] = "",
        repeat: Annotated[str, Form()] = "",
    ) -> Response:
        def fail(message: str, code: int) -> HTMLResponse:
            response = page(
                request,
                "account.html",
                profile=profile,
                nav="account",
                notice=None,
                error=message,
            )
            response.status_code = code
            return response

        key = profile.name.lower()
        if throttle.blocked(key):
            return fail(tr("web.login.blocked"), 429)
        if not auth.verify_password(current, profile.password_hash):
            throttle.failed(key)
            return fail(tr("web.account.wrong_current"), 403)
        if new != repeat:
            return fail(tr("web.account.mismatch"), 422)
        if len(new) < auth.MIN_PASSWORD_LENGTH:
            return fail(tr("web.account.too_short", n=auth.MIN_PASSWORD_LENGTH), 422)
        with session(engine) as s:
            s.get_one(ProfileDB, profile.id).password_hash = auth.hash_password(new)
            auth.end_all_sessions(s, profile.id, keep=request.cookies.get(COOKIE))
        return RedirectResponse("/account?changed=1", status_code=303)

    async def login_required(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/login", status_code=303)

    handler: Callable[[Request, Exception], Any] = login_required
    app.add_exception_handler(LoginRequired, handler)
    app.add_middleware(OriginCheck)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
