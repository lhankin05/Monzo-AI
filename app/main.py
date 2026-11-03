"""Local-only Monzo OAuth test. Tokens are held in RAM and never logged or returned."""
import os
import secrets
import time
import json
import keyring
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()
app = FastAPI(title="Monzo-AI", version="0.3.0")
from app.dashboard import router as dashboard_router
app.include_router(dashboard_router)
_pending_state = None
_pending_at = 0.0
_token = None
_TOKEN_SERVICE = "Monzo-AI-local"
_TOKEN_USER = "oauth-tokens"

def _load_tokens():
    try:
        return json.loads(keyring.get_password(_TOKEN_SERVICE, _TOKEN_USER) or "{}")
    except (ValueError, keyring.errors.KeyringError):
        return {}

def _save_tokens(tokens):
    try:
        keyring.set_password(_TOKEN_SERVICE, _TOKEN_USER, json.dumps(tokens))
    except keyring.errors.KeyringError:
        raise HTTPException(500, "Windows credential storage unavailable; connection not saved")

async def _access_token():
    global _token
    tokens = _load_tokens()
    if _token:
        return _token
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
        _token = tokens["access_token"]
        return _token
    if not tokens.get("refresh_token"):
        return None
    cfg = settings()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.monzo.com/oauth2/token", data={
                "grant_type": "refresh_token", "client_id": cfg["MONZO_CLIENT_ID"],
                "client_secret": cfg["MONZO_CLIENT_SECRET"],
                "refresh_token": tokens["refresh_token"],
            })
            response.raise_for_status()
            fresh = response.json()
        _token = fresh["access_token"]
        _save_tokens({"access_token": _token, "refresh_token": fresh.get("refresh_token", tokens["refresh_token"]),
                      "expires_at": time.time() + int(fresh.get("expires_in", 3600))})
        return _token
    except (httpx.HTTPError, ValueError, KeyError):
        return None


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
        _save_tokens({"access_token": _token, "refresh_token": payload.get("refresh_token"),
                      "expires_at": time.time() + int(payload.get("expires_in", 3600))})
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Monzo token exchange failed. Check developer app settings and try again.")
    return RedirectResponse("/", status_code=303)

@app.get("/monzo/status")
async def monzo_status(request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    token = await _access_token()
    if not token:
        return {"connected": False, "message": "Visit /monzo/connect to authorise."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.monzo.com/accounts", headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            accounts = response.json().get("accounts", [])
        # No account identifiers, balances or transaction data returned.
        return {"connected": True, "accounts_visible": len(accounts), "account_types": sorted(set(str(a.get("type", "unknown")) for a in accounts))}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not retrieve Monzo accounts. The token may need approval or may have expired.")

@app.get("/monzo/transaction-discovery")
async def transaction_discovery(request: Request):
    """Check transaction endpoint support per account without returning personal data."""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    token = await _access_token()
    if not token:
        raise HTTPException(401, "Authorise Monzo first at /monzo/connect")

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.monzo.com/accounts", headers=headers)
            response.raise_for_status()
            accounts = response.json().get("accounts", [])
            for account in accounts:
                account_id = account.get("id")
                if not account_id:
                    continue
                account_type = str(account.get("type", "unknown"))
                try:
                    transactions_response = await client.get(
                        "https://api.monzo.com/transactions",
                        headers=headers,
                        params={"account_id": account_id, "since": since, "limit": 100},
                    )
                    if transactions_response.is_success:
                        transactions = transactions_response.json().get("transactions", [])
                        results.append({
                            "account_type": account_type,
                            "accessible": True,
                            "sample_count": len(transactions),
                            "sample_limited": len(transactions) == 100,
                            "has_flex_related_fields": any(
                                any("flex" in str(key).lower() for key in tx.keys())
                                for tx in transactions if isinstance(tx, dict)
                            ),
                        })
                    else:
                        results.append({
                            "account_type": account_type,
                            "accessible": False,
                            "http_status": transactions_response.status_code,
                        })
                except (httpx.HTTPError, ValueError):
                    results.append({"account_type": account_type, "accessible": False, "error": "request_failed"})
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not retrieve Monzo account list")
    return {"period_days": 30, "accounts": results,
            "note": "Counts reflect up to 100 recent transactions per account, not all purchases. No transaction details are returned."}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")
    connected = bool(await _access_token())
    state = "Connected (authorisation saved on this PC)" if connected else "Not connected"
    return HTMLResponse(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Monzo-AI</title><style>body{font-family:system-ui;background:#10141d;color:#f2f4f8;"
        "max-width:650px;margin:12vh auto;padding:24px}a{display:inline-block;margin:12px 10px 0 0;"
        "padding:12px 18px;background:#293a59;color:white;border-radius:10px;text-decoration:none}"
        "p{color:#c0c8d7}</style></head><body><h1>Monzo-AI</h1><p>" + state +
        "</p><a href='/monzo/connect'>Connect Monzo</a>"
        "<a href='/monzo/status'>Check connection</a>"
        "<a href='/dashboard'>Spending dashboard</a>"
        "<a href='/monzo/transaction-discovery'>Test Flex transactions</a></body></html>"
    )
