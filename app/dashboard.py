"""Local spending dashboard. Raw transactions stay in a local SQLite database."""
import html
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter()
DB = Path(__file__).resolve().parent.parent / "monzo_ai.db"

def local_only(request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Local access only")

def db():
    connection = sqlite3.connect(DB)
    connection.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY, account_type TEXT NOT NULL, account_id TEXT NOT NULL,
        created TEXT NOT NULL, amount INTEGER NOT NULL, description TEXT NOT NULL,
        category TEXT NOT NULL, merchant TEXT NOT NULL
    )""")
    connection.commit()
    return connection

async def sync(token):
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    headers = {"Authorization": f"Bearer {token}"}
    saved = 0
    warnings = []
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get("https://api.monzo.com/accounts", headers=headers)
        response.raise_for_status()
        accounts = response.json().get("accounts", [])
        with db() as conn:
            for account in accounts:
                account_id = account.get("id")
                if not account_id:
                    continue
                kind = str(account.get("type", "unknown"))
                try:
                    res = await client.get("https://api.monzo.com/transactions",
                        headers=headers, params={"account_id": account_id, "since": since, "limit": 100})
                    res.raise_for_status()
                    rows = res.json().get("transactions", [])
                    if len(rows) == 100:
                        warnings.append(kind + ": 100-transaction limit reached; older items may be missing")
                    for tx in rows:
                        if not isinstance(tx, dict) or not tx.get("id"):
                            continue
                        merchant = tx.get("merchant")
                        merchant_name = merchant.get("name", "") if isinstance(merchant, dict) else ""
                        conn.execute("""INSERT INTO transactions
                            (id,account_type,account_id,created,amount,description,category,merchant)
                            VALUES (?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET
                            account_type=excluded.account_type,account_id=excluded.account_id,
                            created=excluded.created,amount=excluded.amount,
                            description=excluded.description,category=excluded.category,
                            merchant=excluded.merchant""",
                            (str(tx["id"]),kind,str(account_id),str(tx.get("created","")),
                             int(tx.get("amount") or 0),str(tx.get("description") or ""),
                             str(tx.get("category") or "uncategorised"),str(merchant_name)))
                        saved += 1
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    warnings.append(kind + ": sync failed (" + type(exc).__name__ + ")")
    return saved, warnings

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    local_only(request)
    from app.main import _access_token
    token = await _access_token()
    if not token:
        return HTMLResponse("<h2>Monzo connection needed</h2><a href='/monzo/connect'>Connect Monzo</a>")
    try:
        saved, warnings = await sync(token)
    except (httpx.HTTPError, ValueError):
        saved, warnings = 0, ["Could not sync accounts. Showing locally cached transactions."]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db() as conn:
        groups = conn.execute("""SELECT account_type, COUNT(*),
            SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END)
            FROM transactions WHERE created >= ? GROUP BY account_type ORDER BY account_type""",
            (cutoff,)).fetchall()
        recent = conn.execute("""SELECT account_type,created,amount,merchant,description,category
            FROM transactions WHERE created >= ? ORDER BY created DESC LIMIT 35""",(cutoff,)).fetchall()
    def esc(value):
        return html.escape(str(value), quote=True)
    cards = "".join("<div class='card'><small>" + esc(kind) + "</small><h2>£" +
        format((pennies or 0)/100, ",.2f") + "</h2><p>" + str(count) +
        " transactions · outgoing entries</p></div>" for kind,count,pennies in groups)
    rows = "".join("<tr><td>" + esc(created[:10]) + "</td><td>" + esc(kind) +
        "</td><td>" + esc(merchant or description) + "</td><td>" + esc(category) +
        "</td><td>£" + format(amount/100, ",.2f") + "</td></tr>"
        for kind,created,amount,merchant,description,category in recent)
    notes = "".join("<p class='warning'>" + esc(w) + "</p>" for w in warnings)
    return HTMLResponse("""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Monzo-AI Spending</title><style>
    body{background:#10141d;color:#edf2fa;font:15px system-ui;margin:0;padding:32px;max-width:1150px;margin:auto}
    h1{font-size:32px}p,small{color:#b8c2d2}.grid{display:flex;flex-wrap:wrap;gap:16px}
    .card{background:#1b2536;border:1px solid #303e55;border-radius:16px;padding:24px;min-width:190px;flex:1}
    h2{font-size:27px;margin:12px 0}a{color:#9ac5ff}table{width:100%;border-collapse:collapse}
    td,th{padding:12px;border-bottom:1px solid #303e55;text-align:left}
    .scroll{overflow-x:auto}.warning{color:#ffca89}
    </style></head><body><h1>Spending overview</h1>
    <p>Last 30 days · synced on opening · local SQLite storage</p>
    <p><a href='/'>Home</a> · <a href='/dashboard'>Refresh &amp; sync</a></p>
    <div class='grid'>""" + (cards or "<p>No transactions yet.</p>") + """</div>
    <p><strong>Important:</strong> Outgoing entries are shown separately by account, not combined.
    Flex repayments, transfers and adjustments may appear as spending. These figures are not
    verified purchase totals and must not be added together.</p>""" + notes +
    "<h2>Recent activity</h2><div class='scroll'><table><thead><tr><th>Date</th><th>Account</th>"
    "<th>Merchant / description</th><th>Category</th><th>Amount</th></tr></thead><tbody>" +
    rows + "</tbody></table></div><p>Data stays on this PC. No AI API calls are made.</p></body></html>")
