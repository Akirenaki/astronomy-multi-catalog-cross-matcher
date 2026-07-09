from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from app.cache import get_object_by_simbad_id, get_or_resolve, list_recent_objects
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database once when the FastAPI app starts up."""
    # Create the database tables before serving requests.
    await init_db()
    yield


app = FastAPI(title="Astronomy Multi-Catalog Cross-Matcher", lifespan=lifespan)
# Load HTML templates from the templates directory so each route can render pages.
env = Environment(loader=FileSystemLoader("app/templates"))


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
    result = await get_or_resolve(q)
    template = env.get_template("result.html")
    html = template.render(request=request, object=result, summary=result.ai_summary or "No summary available.")
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
        obj = await get_or_resolve(simbad_main_id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=obj, summary=obj.ai_summary if obj else "No summary available.")
    return HTMLResponse(content=html)


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
    objects = await list_recent_objects(limit=10)
    template = env.get_template("history.html")
    html = template.render(request=request, objects=objects)
    return HTMLResponse(content=html)
