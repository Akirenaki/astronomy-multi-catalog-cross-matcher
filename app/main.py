"""FastAPI application entry point and route definitions."""

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from markupsafe import Markup
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    DuplicateEmailError,
    authenticate,
    create_user,
    get_current_user,
    get_session_id,
    log_in_session,
    log_out_session,
)
from app.cache import (
    AI_SUMMARY_COOLDOWN,
    CooldownActiveError,
    add_favorite,
    ensure_ai_summary,
    get_cached_ai_summary,
    get_object_by_simbad_id,
    get_object_id_by_simbad_id,
    get_or_resolve,
    is_favorited,
    list_favorites,
    list_recent_objects,
    regenerate_ai_summary,
    remove_favorite,
    save_user_summary_snapshot,
)
from app.database import init_db
from app.models import User
from app.narrative import GeminiGenerationError, GeminiRateLimitedError, render_summary_markdown
from app.ratelimit import RateLimitExceededError, check_limit, record_usage

# Configure root logging so app.* loggers propagate under uvicorn.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database once when the FastAPI app starts up."""
    # Create the database tables before serving requests.
    await init_db()
    yield


app = FastAPI(title="Astronomy Multi-Catalog Cross-Matcher", lifespan=lifespan)

# Session cookies back both login state and anonymous rate limiting.
# Set SESSION_SECRET_KEY in real deployments so cookies survive restarts and work
# consistently across multiple workers.
_session_secret_key = os.getenv("SESSION_SECRET_KEY")
if not _session_secret_key:
    _session_secret_key = secrets.token_hex(32)
    logger.warning(
        "SESSION_SECRET_KEY not set -- using a randomly generated key for this "
        "process only. Sessions will not survive a restart. Set SESSION_SECRET_KEY "
        "explicitly before deploying anywhere beyond local single-process dev."
    )
app.add_middleware(SessionMiddleware, secret_key=_session_secret_key)

# Serve static assets (currently just the hand-rolled design-system CSS -- no
# frontend build step in this project) at /static.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

def _tojson(value) -> Markup:
    """Render a Python value as a JSON literal safe to inline into a <script>
    block. Plain Jinja2 (unlike Flask's) doesn't ship a `tojson` filter, and
    result.html needs one to pass ra_deg/dec_deg/name into the coordinate
    visualiser without hand-rolling JS-string escaping."""
    return Markup(json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


# Load HTML templates from the templates directory so each route can render pages.
# autoescape is REQUIRED here -- a plain jinja2.Environment defaults to
# autoescape=False (unlike FastAPI's Jinja2Templates, which enables it), and
# result.html interpolates object.query_text (raw, user-typed search input)
# into rendered HTML. Without this, that's a reflected XSS: see EVALUATION.md
# 1.1, which reproduced it with a GET /search?q=<img src=x onerror=alert(1)>
# payload rendered unescaped straight into the page <title>.
env = Environment(loader=FileSystemLoader("app/templates"), autoescape=select_autoescape(["html"]))
env.filters["render_summary_markdown"] = render_summary_markdown
env.filters["tojson"] = _tojson


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: User | None = Depends(get_current_user)) -> HTMLResponse:
    """Render the landing page for the web interface."""
    template = env.get_template("index.html")
    html = template.render(request=request, current_user=current_user)
    return HTMLResponse(content=html)


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request, q: str | None = None, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse:
    """Resolve a search query and render the details page, or fall back to the home page when empty."""
    if not q:
        template = env.get_template("index.html")
        html = template.render(request=request, current_user=current_user)
        return HTMLResponse(content=html)

    # Resolve the query through the cache/resolver pipeline and render the result page.
    # generate_ai_summary=False: render the scientific data immediately rather than
    # blocking the whole page on the Gemini call (observed taking up to ~42s for a
    # single heavily-catalogued star). The AI summary panel is filled in afterward by
    # result.html's client-side JS calling GET /object/{id}/summary -- the same
    # "results first, AI overview second" pattern search engines use.
    result = await get_or_resolve(q, generate_ai_summary=False)
    favorited = False
    if current_user is not None and result.id is not None:
        favorited = await is_favorited(current_user.id, result.id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=result, current_user=current_user, favorited=favorited)
    return HTMLResponse(content=html)


@app.get("/object/{simbad_main_id}", response_class=HTMLResponse)
async def object_profile(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse:
    """Display a stored object profile, refreshing it if the cache entry expired."""
    obj = await get_object_by_simbad_id(simbad_main_id)
    if obj is None:
        obj = await get_or_resolve(simbad_main_id, generate_ai_summary=False)
    favorited = False
    if current_user is not None and obj.id is not None:
        favorited = await is_favorited(current_user.id, obj.id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=obj, current_user=current_user, favorited=favorited)
    return HTMLResponse(content=html)


def _gemini_error_response(exc: GeminiGenerationError) -> JSONResponse:
    """Build the JSON body/status for a failed Gemini generation attempt.

    Deliberately uses 503 (Service Unavailable), not 429, even for
    GeminiRateLimitedError: our own app-level throttling (check_limit's
    RateLimitExceededError) and the per-object regenerate cooldown
    (CooldownActiveError) both already use 429 with a {retry_after_seconds} body,
    and the frontend's existing cooldown countdown UI keys off that status code.
    Reusing 429 for "Gemini's servers are overloaded" would make the frontend
    (incorrectly) start that same client-side countdown for a failure that isn't
    actually a countdown-able cooldown on the user's own actions. 503 keeps the
    two failure classes visually and programmatically distinct while still being
    a standard "transient, safe to retry" status.
    """
    body: dict[str, object] = {"error": "ai_generation_failed", "message": exc.user_message}
    if isinstance(exc, GeminiRateLimitedError):
        body["error"] = "ai_rate_limited"
    if exc.retry_after_seconds:
        body["retry_after_seconds"] = exc.retry_after_seconds
    return JSONResponse(body, status_code=503)


@app.get("/object/{simbad_main_id}/summary")
async def object_summary(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> JSONResponse:
    """Return the AI narrative for an object, generating it on demand."""
    # A cache hit costs no Gemini quota, so it must not be charged against the
    # per-client rate limit below -- see EVALUATION.md 1.2. Checked before
    # check_limit() runs at all, not just before record_usage(), since
    # check_limit() alone doesn't write anything but still incorrectly gated a
    # free cache read behind the same budget as an actual generation.
    cached_summary = await get_cached_ai_summary(simbad_main_id)
    if cached_summary is not None:
        # A logged-in user still gets their own personal snapshot of the shared
        # summary on a cache hit, same as on a fresh generation -- this mirrors
        # the un-shortcut path below and is covered by
        # test_ai_summary_remains_single_global_value_regardless_of_snapshot_count.
        if current_user is not None:
            object_id = await get_object_id_by_simbad_id(simbad_main_id)
            if object_id is not None:
                await save_user_summary_snapshot(current_user.id, object_id, cached_summary)
        return JSONResponse({"summary": cached_summary, "summary_html": render_summary_markdown(cached_summary)})

    subject_type = "user" if current_user is not None else "session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id)
    except RateLimitExceededError as exc:
        response = JSONResponse(
            {"error": "Rate limit exceeded", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    try:
        summary = await ensure_ai_summary(simbad_main_id)
    except LookupError:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    except GeminiGenerationError as exc:
        # Attempt failed -- don't record rate-limit usage for it (nothing was
        # actually generated) so the client isn't penalised for a server-side
        # failure that wasn't their fault.
        logger.error("AI summary generation failed for %s: %s", simbad_main_id, exc)
        return _gemini_error_response(exc)

    # Only charge the per-client Gemini-quota-spending allowance once generation
    # has actually succeeded (see app.ratelimit.record_usage's docstring).
    await record_usage(subject_type, subject_id)

    if current_user is not None:
        object_id = await get_object_id_by_simbad_id(simbad_main_id)
        if object_id is not None:
            await save_user_summary_snapshot(current_user.id, object_id, summary)

    return JSONResponse({"summary": summary, "summary_html": render_summary_markdown(summary)})


@app.post("/object/{simbad_main_id}/summary/regenerate")
async def object_summary_regenerate(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> JSONResponse:
    """Regenerate an object's AI narrative, enforcing server-side limits."""
    subject_type = "user" if current_user is not None else "session"
    subject_id = str(current_user.id) if current_user is not None else get_session_id(request)
    try:
        await check_limit(subject_type, subject_id)
    except RateLimitExceededError as exc:
        response = JSONResponse(
            {"error": "Rate limit exceeded", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    try:
        summary = await regenerate_ai_summary(simbad_main_id)
    except LookupError:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    except CooldownActiveError as exc:
        response = JSONResponse(
            {"error": "Cooldown active", "retry_after_seconds": exc.retry_after_seconds},
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response
    except GeminiGenerationError as exc:
        # regenerate_ai_summary() only writes ai_summary_generated_at (the
        # cooldown clock) after generate_summary() returns successfully, so a
        # failure here has already left that timestamp untouched -- the user is
        # free to try again immediately rather than being stuck in a 5-minute
        # cooldown for an attempt that never produced a summary. Likewise, skip
        # record_usage() below so the failed attempt doesn't consume their
        # per-client Gemini-quota-spending allowance either.
        logger.error("AI summary regeneration failed for %s: %s", simbad_main_id, exc)
        return _gemini_error_response(exc)

    await record_usage(subject_type, subject_id)

    if current_user is not None:
        object_id = await get_object_id_by_simbad_id(simbad_main_id)
        if object_id is not None:
            await save_user_summary_snapshot(current_user.id, object_id, summary)

    return JSONResponse({"summary": summary, "cooldown_seconds": int(AI_SUMMARY_COOLDOWN.total_seconds())})


@app.get("/api/resolve")
async def api_resolve(q: str | None = None) -> JSONResponse:
    """Expose the resolver as a JSON API for programmatic access."""
    if not q:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    result = await get_or_resolve(q)
    return JSONResponse(result.to_dict())


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, current_user: User | None = Depends(get_current_user)) -> HTMLResponse:
    """Show the most recently resolved objects from the cache database."""
    objects = list_recent_objects(limit=10)
    if hasattr(objects, "__await__"):
        objects = await objects
    template = env.get_template("history.html")
    html = template.render(request=request, objects=objects, current_user=current_user)
    return HTMLResponse(content=html)


def _safe_next_path(next_path: str | None) -> str:
    """Validate a next= redirect target, defaulting to "/" for anything unsafe.

    Only accepts a path that starts with a single "/" (not "//..." or
    "/\\...", both of which browsers can interpret as protocol-relative URLs
    pointing at an attacker-controlled host) -- this is the standard open-redirect
    guard for a same-origin "return to where you were" parameter.
    """
    if not next_path:
        return "/"
    if not next_path.startswith("/"):
        return "/"
    if next_path.startswith("//") or next_path.startswith("/\\"):
        return "/"
    return next_path


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request, next: str | None = None) -> HTMLResponse:
    """Render the registration form."""
    template = env.get_template("register.html")
    html = template.render(request=request, error=None, next=_safe_next_path(next) if next else None)
    return HTMLResponse(content=html)


@app.post("/register", response_class=HTMLResponse, response_model=None)
async def register_submit(
    request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None)
) -> HTMLResponse | RedirectResponse:
    """Create a new user, hash their password, and log them in on success.

    Duplicate emails and invalid passwords are caught explicitly and re-render the
    form with an error rather than surfacing a raw 500 -- see create_user()'s
    docstring in app/auth.py for the insert-then-catch-IntegrityError pattern this
    relies on.
    """
    normalized_email = email.strip().lower()
    try:
        user = await create_user(email=normalized_email, password=password)
    except DuplicateEmailError:
        template = env.get_template("register.html")
        html = template.render(request=request, error="That email is already registered.", next=next)
        return HTMLResponse(content=html, status_code=400)
    except ValueError as exc:
        template = env.get_template("register.html")
        html = template.render(request=request, error=str(exc), next=next)
        return HTMLResponse(content=html, status_code=400)

    log_in_session(request, user)
    return RedirectResponse(url=_safe_next_path(next), status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    """Render the login form."""
    template = env.get_template("login.html")
    html = template.render(request=request, error=None, next=_safe_next_path(next) if next else None)
    return HTMLResponse(content=html)


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None)
) -> HTMLResponse | RedirectResponse:
    """Verify credentials and log the user in via session on success."""
    user = await authenticate(email.strip().lower(), password)
    if user is None:
        template = env.get_template("login.html")
        html = template.render(request=request, error="Incorrect email or password.", next=next)
        return HTMLResponse(content=html, status_code=400)

    log_in_session(request, user)
    return RedirectResponse(url=_safe_next_path(next), status_code=303)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the current session's logged-in state."""
    log_out_session(request)
    return RedirectResponse(url="/", status_code=303)


@app.post("/object/{simbad_main_id}/favorite", response_model=None)
async def favorite_object(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> RedirectResponse | JSONResponse:
    """Favorite an object for the logged-in user. Requires login; anonymous
    attempts are redirected to the login page rather than silently ignored."""
    if current_user is None:
        return RedirectResponse(url=f"/login?next=/object/{quote(simbad_main_id, safe='')}", status_code=303)

    object_id = await get_object_id_by_simbad_id(simbad_main_id)
    if object_id is None:
        return JSONResponse({"error": "Object not found"}, status_code=404)

    await add_favorite(current_user.id, object_id)
    return RedirectResponse(url=f"/object/{quote(simbad_main_id, safe='')}", status_code=303)


@app.post("/object/{simbad_main_id}/unfavorite", response_model=None)
async def unfavorite_object(
    request: Request, simbad_main_id: str, current_user: User | None = Depends(get_current_user)
) -> RedirectResponse | JSONResponse:
    """Remove an object from the logged-in user's favorites. Requires login."""
    if current_user is None:
        return RedirectResponse(url=f"/login?next=/object/{quote(simbad_main_id, safe='')}", status_code=303)

    object_id = await get_object_id_by_simbad_id(simbad_main_id)
    if object_id is None:
        return JSONResponse({"error": "Object not found"}, status_code=404)

    await remove_favorite(current_user.id, object_id)
    return RedirectResponse(url=f"/object/{quote(simbad_main_id, safe='')}", status_code=303)


@app.get("/account/saved", response_class=HTMLResponse, response_model=None)
async def account_saved(
    request: Request, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse | RedirectResponse:
    """List the logged-in user's favorited objects.

    Each entry shows the user's own personal AI-summary snapshot if they've ever
    generated/regenerated one for that object, falling back to the shared canonical
    ObjectRecord.ai_summary if they favorited an object without ever generating one
    themselves (e.g. they favorited it while someone else's summary was already
    showing, or before any summary existed at all).
    """
    if current_user is None:
        return RedirectResponse(url="/login?next=/account/saved", status_code=303)

    favorites = await list_favorites(current_user.id)
    template = env.get_template("account_saved.html")
    html = template.render(request=request, current_user=current_user, favorites=favorites)
    return HTMLResponse(content=html)
