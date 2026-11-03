"""Local spending dashboard. Raw transactions stay in a local SQLite database."""
import html
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.classification import router as classification_router, render_cards_and_rows
from app.history_sync import sync_history

router = APIRouter()
router.include_router(classification_router)
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
    connection.execute('CREATE TABLE IF NOT EXISTS classifications (transaction_id TEXT PRIMARY KEY, kind TEXT NOT NULL)')
    connection.commit()
    return connection

async def sync(token):
    return await sync_history(token, db)

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
    period = request.query_params.get("period", "30")
    days = {"30":30, "90":90, "365":365, "all":None}.get(period, 30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else "2000-01-01"
    with db() as conn:
        transactions = conn.execute("""SELECT t.id,t.account_type,t.created,t.amount,t.merchant,
            t.description,t.category,c.kind FROM transactions t
            LEFT JOIN classifications c ON c.transaction_id=t.id
            WHERE t.created >= ? ORDER BY t.created DESC""", (cutoff,)).fetchall()
    cards, rows, review_rows, review_count = render_cards_and_rows(transactions)
    def esc(value):
        return html.escape(str(value), quote=True)
    review_section = ("<section class='review'><h2>Needs review (" + str(review_count) +
        ")</h2><p>Classify these transactions here without searching through recent activity.</p>" +
        "<div class='scroll'><table><thead><tr><th>Date</th><th>Account</th><th>Merchant / description</th>" +
        "<th>Category</th><th>Amount</th><th>Type</th></tr></thead><tbody>" +
        review_rows + "</tbody></table></div></section>") if review_count else (
        "<section class='review'><h2>Needs review</h2><p>All caught up — no transactions need review.</p></section>")
    period_links = "<nav class=\'periods\'>" + "".join("<a href=\'/dashboard?period=" + key + "\'" + (" class=\'selected\'" if key == period else "") + ">" + label + "</a>" for key,label in (("30","30 days"),("90","90 days"),("365","1 year"),("all","All time"))) + "</nav>"
    notes = "".join("<p class='warning'>" + esc(w) + "</p>" for w in warnings)
    return HTMLResponse("""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Monzo-AI Spending</title><style>
    body{background:#10141d;color:#edf2fa;font:15px system-ui;margin:0;padding:32px;max-width:1150px;margin:auto}
    h1{font-size:32px}p,small{color:#b8c2d2}.grid{display:flex;flex-wrap:wrap;gap:16px}
    .card{background:#1b2536;border:1px solid #303e55;border-radius:16px;padding:24px;min-width:190px;flex:1}
    h2{font-size:27px;margin:12px 0}a{color:#9ac5ff}table{width:100%;border-collapse:collapse}
    td,th{padding:12px;border-bottom:1px solid #303e55;text-align:left}
.periods{display:flex;gap:8px;margin:16px 0;flex-wrap:wrap}.periods a{padding:9px 13px;background:#263349;border-radius:9px;text-decoration:none}.periods a.selected{background:#4874b2;color:white}
    .filters{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.filters button{background:#263349;color:#edf2fa;border:1px solid #465675;border-radius:9px;padding:9px 13px;cursor:pointer}.filters button.active{background:#4874b2;border-color:#8ebaff}.filters button:hover{border-color:#b6d3ff}
    .scroll{overflow-x:auto}.warning{color:#ffca89}.review{margin-top:24px;padding:18px;background:#1b2536;border:1px solid #465675;border-radius:16px}
    </style></head><body><h1>Spending overview</h1>
    <p>Synced on opening · historical import resumes on refresh · local SQLite storage</p>
    <p><a href='/'>Home</a> · <a href='/dashboard'>Refresh &amp; sync</a></p>
    """ + period_links + """<div class='grid'>""" + (cards or "<p>No transactions yet.</p>") + """</div>
    """ + review_section + """<p><strong>Important:</strong> Classification is provisional. Savings is net of withdrawals. Review uncertain entries and use the dropdown to correct them. Transfers are excluded from spending.</p>""" + notes +
    "<h2>Recent activity (up to 100 newest transactions)</h2><div class='filters' aria-label='Filter recent activity'>" +
    "<button type='button' class='active' data-filter='all' onclick='filterActivity(this)'>All</button>" +
    "<button type='button' data-filter='spending' onclick='filterActivity(this)'>Spending</button>" +
    "<button type='button' data-filter='income' onclick='filterActivity(this)'>Income</button>" +
    "<button type='button' data-filter='savings' onclick='filterActivity(this)'>Net savings</button>" +
    "<button type='button' data-filter='transfer' onclick='filterActivity(this)'>Transfers / repayments</button>" +
    "<button type='button' data-filter='review' onclick='filterActivity(this)'>Needs review</button></div>" +
    "<p id='activity-empty' style='display:none'>No recent transactions in this category.</p>" +
    "<div class='scroll'><table id='activity-table'><thead><tr><th>Date</th><th>Account</th>"
    "<th>Merchant / description</th><th>Category</th><th>Amount</th><th>Type</th></tr></thead><tbody>" +
    rows + """</tbody></table></div>
    <script>
    function filterActivity(button){
      const kind=button.dataset.filter;
      document.querySelectorAll('.filters button').forEach(b=>{
        b.classList.toggle('active',b===button);
        b.setAttribute('aria-pressed',String(b===button));
      });
      let visible=0;
      document.querySelectorAll('#activity-table tbody tr').forEach(row=>{
        const show=kind==='all'||row.dataset.kind===kind;
        row.hidden=!show;
        if(show) visible++;
      });
      document.getElementById('activity-empty').style.display=visible?'none':'block';
    }
    </script><p>Data stays on this PC. No AI API calls are made.</p></body></html>""")
