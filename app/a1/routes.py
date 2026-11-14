from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..db import q, q1
from . import google, service
from .channels import instagram, whatsapp
from .channels.base import verify_meta_signature
from .health import get_health, set_health
from .policy import load_policy, reset_policy, save_policy

log = logging.getLogger("a1.routes")
_basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """Dashboard + operator APIs. If ADMIN_PASSWORD is unset (local dev) the dashboard is open."""
    pw = get_settings().admin_password
    if not pw:
        return
    if creds is None or not hmac.compare_digest(creds.password.encode(), pw.encode()):
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": 'Basic realm="a1"'})


router = APIRouter(prefix="/a1", dependencies=[Depends(require_admin)])
public = APIRouter()   # provider webhooks: authenticated by HMAC signature, not by password


# ------------------------------------------------------------------ pages & read APIs


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    from ..main import templates
    return templates.TemplateResponse(request, "a1.html", {})


@router.get("/api/leads")
def leads():
    return service.lead_summary_rows()


@router.get("/api/leads/{lead_id}")
def lead(lead_id: int):
    d = service.lead_detail(lead_id)
    if d is None:
        raise HTTPException(404, "lead not found")
    return d


@router.get("/api/health")
def health():
    s = get_settings()
    h = get_health()
    configured = {"whatsapp": whatsapp.configured(), "instagram": instagram.configured(), "email": google.configured(), "calendar": google.configured()}
    needs = {
        "whatsapp": "WA_VERIFY_TOKEN, WA_APP_SECRET, WA_ACCESS_TOKEN, WA_PHONE_NUMBER_ID",
        "instagram": "IG_VERIFY_TOKEN, IG_APP_SECRET, IG_ACCESS_TOKEN, IG_ACCOUNT_ID",
        "email": "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET + connect Google",
        "calendar": "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET + connect Google",
    }
    out = {}
    for ch in ("whatsapp", "instagram", "email", "calendar"):
        row = h.get(ch, {})
        status = row.get("status", "configured, no traffic yet" if configured[ch] else "unconfigured")
        detail = row.get("detail") or (None if configured[ch] else "missing: " + needs[ch])
        if ch in ("email", "calendar") and configured[ch] and not google.connected():
            status = "expired" if q1("SELECT 1 FROM kv WHERE key='google_expired'") else "not connected"
            detail = "Google is not connected" if status == "not connected" else (detail or "Google authorisation expired or revoked")
        out[ch] = {"status": status, "detail": detail, "last_inbound_at": row.get("last_inbound_at"), "last_outbound_at": row.get("last_outbound_at")}
    usage = q1("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE scope LIKE 'a1:%'")
    return {
        "channels": out,
        "google_account": google.profile_email() if google.connected() and google.configured() else None,
        "webhooks": {"whatsapp": s.base_url.rstrip("/") + "/webhooks/whatsapp", "instagram": s.base_url.rstrip("/") + "/webhooks/instagram",
                     "google_redirect_uri": google.redirect_uri()},
        "usage": {"calls": usage["calls"], "input_tokens": usage["i"], "output_tokens": usage["o"], "cost_usd": round(usage["c"], 4)},
    }


# ------------------------------------------------------------------ operator actions


class Takeover(BaseModel):
    enabled: bool  # automation enabled?


class HumanMsg(BaseModel):
    text: str


@router.post("/api/leads/{lead_id}/automation")
def automation(lead_id: int, body: Takeover):
    if not q1("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        raise HTTPException(404)
    service.set_automation(lead_id, body.enabled)
    return {"ok": True}


@router.post("/api/leads/{lead_id}/message")
def human_message(lead_id: int, body: HumanMsg):
    if not q1("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        raise HTTPException(404)
    if not body.text.strip():
        raise HTTPException(422, "empty message")
    return {"sent": service.human_message(lead_id, body.text.strip())}


@router.post("/api/leads/{lead_id}/retry")
def retry(lead_id: int, bg: BackgroundTasks):
    if not q1("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        raise HTTPException(404)
    bg.add_task(service.retry, lead_id)
    return {"ok": True}


@router.post("/api/leads/{lead_id}/reconcile")
def reconcile(lead_id: int):
    if not q1("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        raise HTTPException(404)
    return {"result": service.reconcile_booking(lead_id)}


@router.post("/api/poll-email")
def poll_email_now():
    return {"processed": service.poll_gmail_once()}


# ------------------------------------------------------------------ policy


@router.get("/api/policy")
def get_policy():
    return load_policy().model_dump(mode="json")


@router.put("/api/policy")
async def put_policy(request: Request):
    raw = (await request.body()).decode()
    try:
        return save_policy(raw).model_dump(mode="json")
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e)[:600])


@router.post("/api/policy/reset")
def policy_reset():
    return reset_policy().model_dump(mode="json")


# ------------------------------------------------------------------ Google OAuth


def _sign(nonce: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:32]


@router.get("/auth/google/start")
def google_start():
    if not google.configured():
        raise HTTPException(400, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
    nonce = secrets.token_urlsafe(16)
    resp = RedirectResponse(google.auth_url(f"{nonce}.{_sign(nonce)}"))
    resp.set_cookie("g_state", nonce, httponly=True, samesite="lax", max_age=600, secure=get_settings().base_url.startswith("https"))
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: Optional[str] = None, state: str = "", error: Optional[str] = None):
    if error:
        raise HTTPException(400, f"Google returned an error: {error}")
    nonce, _, sig = state.partition(".")
    if not code or not nonce or nonce != request.cookies.get("g_state") or not hmac.compare_digest(sig, _sign(nonce)):
        raise HTTPException(400, "invalid OAuth state")
    try:
        google.exchange_code(code)
        email = google.profile_email()
    except (google.ApiError, google.AuthExpired) as e:
        raise HTTPException(502, f"token exchange failed: {e}")
    if not q1("SELECT 1 FROM kv WHERE key='google_refresh_token'"):
        raise HTTPException(400, "Google did not return a refresh token; revoke the app at myaccount.google.com/permissions and connect again")
    set_health("email", "ok", f"connected as {email}")
    set_health("calendar", "ok", f"connected as {email}")
    return RedirectResponse("/a1")


@router.post("/auth/google/disconnect")
def google_disconnect():
    google.disconnect()
    set_health("email", "not connected", "disconnected by operator")
    set_health("calendar", "not connected", "disconnected by operator")
    return {"ok": True}


# ------------------------------------------------------------------ provider webhooks (public, signature-verified)


def _meta_webhook_verify(mode: str | None, token: str | None, challenge: str | None, expected: str) -> Response:
    if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "verification failed")


@public.get("/webhooks/whatsapp")
def wa_verify(hub_mode: str | None = Query(None, alias="hub.mode"), hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
              hub_challenge: str | None = Query(None, alias="hub.challenge")):
    return _meta_webhook_verify(hub_mode, hub_verify_token, hub_challenge, get_settings().wa_verify_token)


@public.post("/webhooks/whatsapp")
async def wa_receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not verify_meta_signature(raw, request.headers.get("x-hub-signature-256"), get_settings().wa_app_secret):
        set_health("whatsapp", "error", "webhook signature verification failed (wrong WA_APP_SECRET or forged request)")
        raise HTTPException(403, "bad signature")
    try:
        inbound = whatsapp.parse(await request.json())
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    for i in inbound:
        bg.add_task(_safe_handle, i)
    return {"received": len(inbound)}


@public.get("/webhooks/instagram")
def ig_verify(hub_mode: str | None = Query(None, alias="hub.mode"), hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
              hub_challenge: str | None = Query(None, alias="hub.challenge")):
    return _meta_webhook_verify(hub_mode, hub_verify_token, hub_challenge, get_settings().ig_verify_token)


@public.post("/webhooks/instagram")
async def ig_receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not verify_meta_signature(raw, request.headers.get("x-hub-signature-256"), get_settings().ig_app_secret):
        set_health("instagram", "error", "webhook signature verification failed (wrong IG_APP_SECRET or forged request)")
        raise HTTPException(403, "bad signature")
    try:
        inbound = instagram.parse(await request.json())
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    for i in inbound:
        bg.add_task(_safe_handle, i)
    return {"received": len(inbound)}


def _safe_handle(inb) -> None:
    try:
        service.handle_inbound(inb)
    except Exception:  # noqa: BLE001 - a webhook worker must never crash the process; the error is logged and shown
        log.exception("inbound handling failed")
        from .health import log as alog
        alog(None, "internal_error", f"unhandled error processing {inb.channel} message {inb.provider_msg_id}; see server logs", "error")
