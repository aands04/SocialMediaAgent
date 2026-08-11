from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _legal_context(request: Request, *, page: str) -> dict[str, object]:
    settings = get_settings()
    return {
        "request": request,
        "page": page,
        "controller_name": settings.legal_controller_name,
        "controller_street": settings.legal_controller_street,
        "controller_city": settings.legal_controller_city,
        "contact_email": settings.legal_contact_email,
        "imprint_url": settings.legal_imprint_url,
        "effective_date": settings.legal_privacy_effective_date,
        "robots": "noindex,follow",
    }


def _public_legal_response(response: HTMLResponse) -> HTMLResponse:
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@router.get("/datenschutz", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy(request: Request):
    return _public_legal_response(
        templates.TemplateResponse(
            request,
            "legal/privacy.html",
            _legal_context(request, page="privacy"),
        )
    )


@router.get("/datenloeschung", response_class=HTMLResponse, include_in_schema=False)
def data_deletion(request: Request):
    return _public_legal_response(
        templates.TemplateResponse(
            request,
            "legal/data_deletion.html",
            _legal_context(request, page="deletion"),
        )
    )
