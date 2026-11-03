"""Transaction classification: conservative defaults and persistent local corrections."""
import html
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
KINDS = ("spending", "income", "savings", "transfer", "review")

def classify(account, amount, category, description):
    cat = (category or "").lower().strip().replace(" ", "_")
    desc = (description or "").lower()
    if cat in ("savings", "saving") or "savings account" in desc:
        return "savings"
    if cat in ("transfers", "transfer") or "flex repayment" in desc:
        return "transfer"
    if amount > 0:
        return "income" if cat in ("income", "salary") else "review"
    if amount == 0:
        return "review"
    if cat in ("shopping", "groceries", "eating_out", "entertainment",
               "transport", "bills", "general", "expenses", "personal_care", "holidays"):
        return "spending"
    return "review"

def render_cards_and_rows(transactions):
    totals = {kind: 0 for kind in KINDS}
    counts = {kind: 0 for kind in KINDS}
    rendered = []
    review_rows = []
    esc = lambda value: html.escape(str(value), quote=True)
    for txid, account, created, amount, merchant, description, category, override in transactions:
        kind = override or classify(account, amount, category, description)
        counts[kind] += 1
        if kind == "spending" and amount < 0:
            totals[kind] -= amount
        elif kind == "income" and amount > 0:
            totals[kind] += amount
        elif kind == "savings":
            totals[kind] -= amount
        else:
            totals[kind] += abs(amount)
        if kind == "review" or len(rendered) < 100:
            options = "".join("<option value='" + k + "'" + (" selected" if k == kind else "") +
                              ">" + k.title() + "</option>" for k in KINDS)
            row = "<tr><td>" + esc(created[:10]) + "</td><td>" + esc(account) +
                "</td><td>" + esc(merchant or description) + "</td><td>" + esc(category) +
                "</td><td>£" + format(amount/100, ",.2f") +
                "</td><td><form method='post' action='/dashboard/classify'>" +
                "<input type='hidden' name='transaction_id' value='" + esc(txid) + "'>" +
                "<select name='kind'>" + options + "</select> <button>Save</button></form></td></tr>"
            if kind == "review":
                review_rows.append(row)
            if len(rendered) < 100:
                rendered.append(row)
    labels = (("spending","Spending"), ("income","Income"), ("savings","Net savings"),
              ("transfer","Transfers / repayments"), ("review","Needs review"))
    cards = "".join("<div class='card'><small>" + label + "</small><h2>" +
                    ("−" if totals[k] < 0 else "") + "£" + format(abs(totals[k])/100, ",.2f") +
                    "</h2><p>" + str(counts[k]) + " entries</p></div>" for k,label in labels)
    return cards, "".join(rendered), "".join(review_rows), len(review_rows)

@router.post("/dashboard/classify")
async def set_classification(request: Request):
    from app.dashboard import db, local_only
    local_only(request)
    form = await request.form()
    txid, kind = str(form.get("transaction_id", "")), str(form.get("kind", ""))
    if kind not in KINDS or not txid or len(txid) > 256:
        raise HTTPException(400, "Invalid classification")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM transactions WHERE id=?", (txid,)).fetchone():
            raise HTTPException(404, "Transaction not found")
        conn.execute("INSERT INTO classifications(transaction_id,kind) VALUES (?,?) "
                     "ON CONFLICT(transaction_id) DO UPDATE SET kind=excluded.kind", (txid,kind))
    return RedirectResponse("/dashboard", status_code=303)
