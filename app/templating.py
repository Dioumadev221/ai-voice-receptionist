from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import get_settings

env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    s = get_settings()
    base = {
        "clinic": s.clinic_name,
        "public_url": (s.public_url or str(request.base_url)).rstrip("/").replace("http://", "https://")
        if "localhost" not in str(request.base_url) else str(request.base_url).rstrip("/"),
        "request_path": request.url.path,
        "year": datetime.now().year,
    }
    return HTMLResponse(env.get_template(template).render(**base, **ctx))
