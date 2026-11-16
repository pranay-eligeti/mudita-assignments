"""One FastAPI application, two routes: /a1 (sales assistant) and /a2 (three-agent coordinator)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
log = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .a2 import orchestrator as a2
    from .a1 import service as a1

    n = a2.recover_interrupted()
    if n:
        log.warning("marked %d interrupted A2 run(s) as failed/resumable", n)
    task = None
    if get_settings().gmail_polling_enabled:
        task = asyncio.create_task(a1.gmail_poll_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Mudita AI Generalist assignments", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

from .a2.routes import router as a2_router  # noqa: E402
from .a1.routes import router as a1_router, public as a1_public  # noqa: E402

app.include_router(a2_router)
app.include_router(a1_router)
app.include_router(a1_public)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.api_route("/healthz", methods=["GET", "HEAD"])  # HEAD: uptime monitors probe with it
def healthz():
    return {"ok": True}
