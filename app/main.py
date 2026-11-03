"""Local-only Monzo OAuth test. Tokens are held in RAM and never logged or returned."""
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()
app = FastAPI(title="Monzo-AI", version="0.2.0")
_pending_state = None
_pending_at = 0.0
_token = None

def settings():
    return {k: os.getenv(k, "").strip() for k in ("MONZO_CLIENT_ID", "MONZO_CLIENT_SECRET", "MONZO_REDIRECT_URI")}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/config-status")
def config_status():
    return {"configured": {k: bool(v) for k, v in settings().items()}}

@app.get("/monzo/connect")
def connect(request: Request):
    global _pending_state, _pending_at
    cfg = settings()
    if not all(cfg.values()):
        raise HTTPException(400, "Configure Monzo credentials in .env first")
    # Never accept OAuth initiation from a remote machine.
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    redirect = urlparse(cfg["MONZO_REDIRECT_URI"])
    if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1") or redirect.port != 8765 or redirect.path != "/callback":
        raise HTTPException(400, "Redirect URI must be http://localhost:8765/callback or http://127.0.0.1:8765/callback")
    _pending_state = secrets.token_urlsafe(32)
    _pending_at = time.monotonic()
    query = urlencode({"client_id": cfg["MONZO_CLIENT_ID"], "redirect_uri": cfg["MONZO_REDIRECT_URI"], "response_type": "code", "state": _pending_state})
    return RedirectResponse("https://auth.monzo.com/?" + query)

@app.get("/callback", response_class=HTMLResponse)
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    global _pending_state, _token
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    if error:
        return HTMLResponse("<h2>Monzo authorisation was not completed.</h2>", status_code=400)
    if not _pending_state or not secrets.compare_digest(state, _pending_state) or time.monotonic() - _pending_at > 600 or not code:
        raise HTTPException(400, "Invalid or expired authorisation response. Start again at /monzo/connect")
    _pending_state = None
    cfg = settings()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.monzo.com/oauth2/token", data={
                "grant_type": "authorization_code", "client_id": cfg["MONZO_CLIENT_ID"],
                "client_secret": cfg["MONZO_CLIENT_SECRET"], "redirect_uri": cfg["MONZO_REDIRECT_URI"],
                "code": code,
            })
            response.raise_for_status()
            payload = response.json()
        if not payload.get("access_token"):
            raise ValueError("Missing access token")
        _token = payload["access_token"]
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Monzo token exchange failed. Check developer app settings and try again.")
    return HTMLResponse("<h2>Monzo authorised.</h2><p>Token is held only in server memory. Open /monzo/status to test account access.</p>")

@app.get("/monzo/status")
async def monzo_status(request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    if not _token:
        return {"connected": False, "message": "Visit /monzo/connect to authorise."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.monzo.com/accounts", headers={"Authorization": f"Bearer {_token}"})
            response.raise_for_status()
            accounts = response.json().get("accounts", [])
        # No account identifiers, balances or transaction data returned.
        return {"connected": True, "accounts_visible": len(accounts), "account_types": sorted(set(str(a.get("type", "unknown")) for a in accounts))}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not retrieve Monzo accounts. The token may need approval or may have expired.")
