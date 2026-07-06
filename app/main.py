from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from app.cache import get_object_by_simbad_id, get_or_resolve, list_recent_objects
from app.database import init_db

app = FastAPI(title="Astronomy Multi-Catalog Cross-Matcher")
env = Environment(loader=FileSystemLoader("app/templates"))


@app.on_event("startup")
async def startup_event() -> None:
    await init_db()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    template = env.get_template("index.html")
    html = template.render(request=request)
    return HTMLResponse(content=html)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str | None = None) -> HTMLResponse:
    if not q:
        template = env.get_template("index.html")
        html = template.render(request=request)
        return HTMLResponse(content=html)

    result = await get_or_resolve(q)
    template = env.get_template("result.html")
    html = template.render(request=request, object=result, summary=result.ai_summary or "No summary available.")
    return HTMLResponse(content=html)


@app.get("/object/{simbad_main_id}", response_class=HTMLResponse)
async def object_profile(request: Request, simbad_main_id: str) -> HTMLResponse:
    obj = await get_object_by_simbad_id(simbad_main_id)
    template = env.get_template("result.html")
    html = template.render(request=request, object=obj, summary=obj.ai_summary if obj else "No summary available.")
    return HTMLResponse(content=html)


@app.get("/api/resolve")
async def api_resolve(q: str | None = None) -> JSONResponse:
    if not q:
        return JSONResponse({"error": "Missing query"}, status_code=400)
    result = await get_or_resolve(q)
    return JSONResponse(result.to_dict())


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request) -> HTMLResponse:
    objects = await list_recent_objects(limit=10)
    template = env.get_template("history.html")
    html = template.render(request=request, objects=objects)
    return HTMLResponse(content=html)
