"""FastAPI application entry point and route definitions."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from app.cache import (
    AI_SUMMARY_COOLDOWN,
    CooldownActiveError,
    ensure_ai_summary,
    get_object_by_simbad_id,
    get_or_resolve,
    list_recent_objects,
    regenerate_ai_summary,
)
from app.database import init_db
from app.narrative import render_summary_markdown

# uvicorn's default logging config only sets up its own "uvicorn"/"uvicorn.error"/
# "uvicorn.access" loggers -- it does not touch the root logger. Every app.* module
# (resolver, cache, catalogs.simbad, catalogs.exoplanet_archive) creates its logger via
# plain logging.getLogger(__name__) and relies on propagation to root, so without this,
# every logger.info()/logger.warning() call in this app -- including the per-stage
# timing diagnostics used to debug slow searches -- is silently discarded rather than
# printed anywhere, even when running with `uvicorn app.main:app --reload`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database once when the FastAPI app starts up."""
    # Create the database tables before serving requests.
    await init_db()
    yield


app = FastAPI(title="Astronomy Multi-Catalog Cross-Matcher", lifespan=lifespan)
# Load HTML templates from the templates directory so each route can render pages.
env = Environment(loader=FileSystemLoader("app/templates"))
env.filters["render_summary_markdown"] = render_summary_markdown


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """Render the landing page for the web interface."""
    template = env.get_template("index.html")
    html = template.render(request=request)
    return HTMLResponse(content=html)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str | None = None) -> HTMLResponse:
    """Resolve a search query and render the details page, or fall back to the home page when empty."""
    if not q:
        template = env.get_template("index.html")
        html = template.render(request=request)
        return HTMLResponse(content=html)

    # Resolve the query through the cache/resolver pipeline and render the result page.
    # generate_ai_summary=False: render the scientific data immediately rather than
    # blocking the whole page on the Gemini call (observed taking up to ~42s for a
    # single heavily-catalogued star). The AI summary panel is filled in afterward by
    # result.html's client-side JS calling GET /object/{id}/summary -- the same
    # "results first, AI overview second" pattern search engines use.
    result = await get_or_resolve(q, generate_ai_summary=False)
    template = env.get_template("result.html")
    html = template.render(request=request, object=result)
    return HTMLResponse(content=html)


@app.get("/object/{simbad_main_id}", response_class=HTMLResponse)
async def object_profile(request: Request, simbad_main_id: str) -> HTMLResponse:
    """Display a previously stored object profile using its SIMBAD identifier.

    Honors the same TTL as /search: if the cached row for this id has expired (or
    was never cached under this exact id), re-resolve using the id itself, which is
    always a valid SIMBAD identifier, rather than silently serving stale/blank data.
    """
    obj = await get_object_by_simbad_id(simbad_main_id)
    if obj is None:
        obj = await get_or_resolve(simbad_main_id, generate_ai_summary=False)
    template = env.get_template("result.html")
    html = template.render(request=request, object=obj)
    return HTMLResponse(content=html)


@app.get("/object/{simbad_main_id}/summary")
async def object_summary(simbad_main_id: str) -> JSONResponse:
    """Lazily generate (or return the already-cached) AI narrative for an object.

    Called by result.html's client-side JS strictly after the main page has already
    rendered with the scientific data, so a slow Gemini call never blocks the page
    the user is actually waiting on. See ensure_ai_summary()'s docstring for the
    caching/skip rules this follows.
    """
    try:
        summary = await ensure_ai_summary(simbad_main_id)
    except LookupError:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    return JSONResponse({"summary": summary, "summary_html": render_summary_markdown(summary)})


@app.post("/object/{simbad_main_id}/summary/regenerate")
async def object_summary_regenerate(simbad_main_id: str) -> JSONResponse:
    """Force-regenerate the AI narrative for an object, subject to a per-object
    cooldown. See regenerate_ai_summary()'s docstring (app/cache.py) for exactly
    what this cooldown does and doesn't protect against -- it's a UX guard against
    double-clicking, not abuse protection, since the underlying cache is global.

    The Retry-After header and JSON body's retry_after_seconds are the
    server-side source of truth the client-side countdown in result.html is built
    from; disabling the button client-side alone would be trivially bypassed by
    calling this endpoint directly, so the 429 here is the actual enforcement.
    """
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
    return JSONResponse({"summary": summary, "cooldown_seconds": int(AI_SUMMARY_COOLDOWN.total_seconds())})


@app.get("/api/resolve")
async def api_resolve(q: str | None = None) -> JSONResponse:
    """Expose the resolver as a JSON API for programmatic access."""
    if not q:
        return JSONResponse({"error": "Missing query"}, status_code=400)

    result = await get_or_resolve(q)
    return JSONResponse(result.to_dict())


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request) -> HTMLResponse:
    """Show the most recently resolved objects from the cache database."""
    objects = list_recent_objects(limit=10)
    if hasattr(objects, "__await__"):
        objects = await objects
    template = env.get_template("history.html")
    html = template.render(request=request, objects=objects)
    return HTMLResponse(content=html)
