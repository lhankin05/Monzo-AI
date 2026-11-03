"""Paginated Monzo history import. Local-only, resumable across dashboard visits."""
from datetime import datetime, timedelta, timezone
import httpx

PAGE_SIZE = 100
MAX_PAGES_PER_VISIT = 20
EARLIEST = "2000-01-01T00:00:00Z"

async def sync_history(token, db):
    headers = {"Authorization": "Bearer " + token}
    warnings = []
    imported = 0
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get("https://api.monzo.com/accounts", headers=headers)
        response.raise_for_status()
        accounts = response.json().get("accounts", [])
        with db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sync_state (
                account_id TEXT PRIMARY KEY, before_cursor TEXT, complete INTEGER NOT NULL DEFAULT 0
            )""")
            for account in accounts:
                aid = account.get("id")
                if not aid:
                    continue
                kind = str(account.get("type", "unknown"))
                state = conn.execute("SELECT before_cursor,complete FROM sync_state WHERE account_id=?", (aid,)).fetchone()
                full_complete = bool(state and state[1])
                cursor = state[0] if state else None
                # Completed accounts refresh recent transactions; incomplete accounts continue backwards.
                now = datetime.now(timezone.utc)
                recent_since = (now - timedelta(days=7)).isoformat()
                pages = 0
                try:
                    while pages < MAX_PAGES_PER_VISIT:
                        params = {"account_id": aid, "limit": PAGE_SIZE}
                        if full_complete:
                            params["since"] = recent_since
                        else:
                            params["since"] = EARLIEST
                            if cursor:
                                params["before"] = cursor
                        res = await client.get("https://api.monzo.com/transactions", headers=headers, params=params)
                        res.raise_for_status()
                        rows = res.json().get("transactions", [])
                        if not isinstance(rows, list):
                            raise ValueError("Invalid transactions response")
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
                                (str(tx["id"]),kind,str(aid),str(tx.get("created","")),
                                 int(tx.get("amount") or 0),str(tx.get("description") or ""),
                                 str(tx.get("category") or "uncategorised"),str(merchant_name)))
                            imported += 1
                        conn.commit()
                        pages += 1
                        if full_complete:
                            if len(rows) == PAGE_SIZE:
                                warnings.append(kind + ": recent sync reached page limit; check again later")
                            break
                        if len(rows) < PAGE_SIZE:
                            conn.execute("""INSERT INTO sync_state(account_id,before_cursor,complete)
                                VALUES (?,?,1) ON CONFLICT(account_id) DO UPDATE SET
                                before_cursor=excluded.before_cursor,complete=1""",(aid,cursor))
                            conn.commit()
                            full_complete = True
                            break
                        oldest = min((str(tx.get("created") or "") for tx in rows if isinstance(tx,dict)), default="")
                        if not oldest or (cursor and oldest >= cursor):
                            warnings.append(kind + ": history pagination stopped; cursor did not advance")
                            break
                        cursor = oldest
                        conn.execute("""INSERT INTO sync_state(account_id,before_cursor,complete)
                            VALUES (?,?,0) ON CONFLICT(account_id) DO UPDATE SET
                            before_cursor=excluded.before_cursor,complete=0""",(aid,cursor))
                        conn.commit()
                    if not full_complete:
                        warnings.append(kind + ": historical import in progress; refresh dashboard to continue")
                except (httpx.HTTPError, ValueError, TypeError):
                    warnings.append(kind + ": sync interrupted; saved progress retained")
    return imported, warnings
