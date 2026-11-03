"""Paginated Monzo history import. Local-only, resumable across dashboard visits."""
from datetime import datetime, timedelta, timezone
import httpx

PAGE_SIZE = 100
MAX_PAGES_PER_VISIT = 20
EARLIEST = datetime(2015, 1, 1, tzinfo=timezone.utc)
WINDOW = timedelta(days=7)

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
                            end = datetime.fromisoformat(cursor.replace("Z", "+00:00")) if cursor else now
                            start = max(EARLIEST, end - WINDOW)
                            params["since"] = start.isoformat()
                            params["before"] = end.isoformat()
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
                        # Move to the previous bounded date window. If a window fills the
                        # page, narrow it on the next visit instead of silently losing rows.
                        if len(rows) == PAGE_SIZE:
                            warnings.append(kind + ": a seven-day window reached 100 transactions; narrowing the window is required")
                            break
                        cursor = start.isoformat()
                        full_complete = start <= EARLIEST
                        conn.execute("""INSERT INTO sync_state(account_id,before_cursor,complete)
                            VALUES (?,?,?) ON CONFLICT(account_id) DO UPDATE SET
                            before_cursor=excluded.before_cursor,complete=excluded.complete""",
                            (aid,cursor,int(full_complete)))
                        conn.commit()
                        if full_complete:
                            break
                    if not full_complete:
                        warnings.append(kind + ": historical import in progress; refresh dashboard to continue")
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (401, 403):
                        warnings.append(kind + ": Monzo denied history access (HTTP " + str(status) +
                                        "). Reconnect Monzo if authorisation expired; older history may be restricted.")
                    elif status == 429:
                        warnings.append(kind + ": Monzo rate limit (HTTP 429). Wait before refreshing.")
                    else:
                        detail = ""
                        try:
                            payload = exc.response.json()
                            if isinstance(payload, dict):
                                msg = payload.get("message") or payload.get("error_description") or payload.get("error")
                                if isinstance(msg, str):
                                    detail = ": " + msg[:160].replace(chr(10), " ").replace(chr(13), " ")
                        except (ValueError, TypeError):
                            pass
                        warnings.append(kind + ": history request rejected (HTTP " + str(status) +
                                        ")" + detail + ". Saved progress retained.")
                except httpx.TimeoutException:
                    warnings.append(kind + ": Monzo request timed out. Saved progress retained; refresh to retry.")
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    warnings.append(kind + ": history sync failed (" + type(exc).__name__ +
                                    "). Saved progress retained.")
    return imported, warnings
