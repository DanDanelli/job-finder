"""The site: sign-in, jobs and profile pages (Jinja templates, plain forms).

Protection:
- every page except /login needs a session; a profile only ever sees its own data;
- the session cookie is HttpOnly, SameSite=Lax and (by default) Secure;
- POSTs from another origin are refused (CSRF), on top of SameSite.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Form, Request
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
from job_finder.telegram.digest import PERIODS, WORK_MODES, short_places
from job_finder.web import auth, data, forms

HERE = Path(__file__).parent
COOKIE = "jf_session"
SCORE_STEPS = (0, 50, 60, 70, 80)


class LoginRequired(Exception):
    pass


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser form (or a very old browser): SameSite still applies
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    return urlsplit(origin).netloc in hosts - {None}


class OriginCheck(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and not _same_origin(request):
            return Response("cross-origin request refused", status_code=403)
        return await call_next(request)


def mount_site(app: FastAPI, engine: Engine, settings: Settings) -> None:
    lang = settings.language
    templates = Jinja2Templates(directory=HERE / "templates")
    throttle = auth.LoginThrottle()
    router = APIRouter()

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

    templates.env.globals.update(
        t=tr,
        lang=lang,
        salary=salary,
        work_mode=work_mode,
        short_places=short_places,
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
    ) -> HTMLResponse:
        tab = tab if tab in data.TABS else "new"
        cards = data.job_cards(engine, profile.id, tab, min_score=min_score, work_mode=mode or None)
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
            score_steps=SCORE_STEPS,
            query=urlencode({"tab": tab, "min_score": min_score, "mode": mode}),
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

    async def login_required(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/login", status_code=303)

    handler: Callable[[Request, Exception], Any] = login_required
    app.add_exception_handler(LoginRequired, handler)
    app.add_middleware(OriginCheck)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
