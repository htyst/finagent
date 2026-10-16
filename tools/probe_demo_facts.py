"""Read the local demo SQLite and print hand-checkable facts for manual test cases.

Read-only. Never writes to the database (opened with mode=ro).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\finagent\var\finagent.sqlite3")
if not DB.exists():
    raise SystemExit(f"database not found: {DB}")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
q = con.execute


def rows(sql: str, *args):
    return [dict(r) for r in q(sql, args).fetchall()]


out: dict = {"db": str(DB), "db_bytes": DB.stat().st_size}
out["tables"] = [r["name"] for r in rows("select name from sqlite_master where type='table' order by name")]


def table_exists(name: str) -> bool:
    return name in out["tables"]


def first_table(*candidates: str) -> str | None:
    for name in candidates:
        if table_exists(name):
            return name
    return None


def safe(label: str, sql: str, *args) -> None:
    try:
        out[label] = rows(sql, *args)
    except sqlite3.Error as exc:
        out[label] = {"error": str(exc)}


TBL = {
    key: first_table(*names)
    for key, names in {
        "setting": ("demo_setting", "demo_settings"),
        "account": ("account", "accounts"),
        "card": ("card", "cards"),
        "recipient": ("recipient", "recipients"),
        "user": ("user", "users"),
        "txn": ("transaction", "transactions"),
        "coverage": ("data_coverage",),
    }.items()
}
out["resolved_tables"] = TBL


def q_ident(key: str) -> str:
    name = TBL.get(key)
    if not name:
        raise sqlite3.OperationalError(f"no table for {key}")
    return f'"{name}"'


setting = rows(f"select * from {q_ident('setting')} limit 1") if TBL["setting"] else []
out["demo_setting"] = setting[0] if setting else None
TXN = q_ident("txn") if TBL["txn"] else None
out["txn_table"] = TBL["txn"]

out["segments"] = rows(
    f"""
    select t.owner_id, t.account_id, t.type, t.status, t.direction, sum(t.amount_minor) as total_minor, count(*) as n
    from {TXN} t group by 1,2,3,4,5 order by 1,2,3,4,5
    """
) if TXN else []

out["accounts"] = rows(
    f"select id, owner_id, alias, account_last4, balance_minor, opening_balance_minor, status, version from {q_ident('account')} order by id"
)
out["cards"] = rows(f"select id, owner_id, account_id, card_last4, status from {q_ident('card')} order by id")
out["recipients"] = rows(f"select id, owner_id, name, account_last4, status from {q_ident('recipient')} order by id")
out["users"] = rows(f"select id, display_name from {q_ident('user')} order by id")

# Ledger identity per account: balance == opening + posted credit - posted debit
out["ledger_check"] = rows(
    f"""
    select a.id as account_id, a.balance_minor,
           a.opening_balance_minor
           + coalesce(sum(case when t.status='posted' and t.direction='credit' then t.amount_minor else 0 end),0)
           - coalesce(sum(case when t.status='posted' and t.direction='debit'  then t.amount_minor else 0 end),0) as derived,
           a.balance_minor - (a.opening_balance_minor
           + coalesce(sum(case when t.status='posted' and t.direction='credit' then t.amount_minor else 0 end),0)
           - coalesce(sum(case when t.status='posted' and t.direction='debit'  then t.amount_minor else 0 end),0)) as diff
    from {q_ident('account')} a left join {TXN} t on t.account_id = a.id
    group by a.id order by a.id
    """
)

# Demo-date month window (2026-09-01 .. 2026-09-25 Shanghai = Aug-31 16:00Z .. Sep-24 16:00Z) for u1
out["u1_net_spending_this_window"] = rows(
    f"""
    select coalesce(category,'未分类') as category,
           sum(case when type='purchase' and status='posted' then amount_minor else 0 end) as purchases_minor,
           sum(case when type='refund'   and status='posted' then amount_minor else 0 end) as refunds_minor,
           sum(case when type='purchase' and status='posted' then amount_minor else 0 end)
         - sum(case when type='refund'   and status='posted' then amount_minor else 0 end) as net_minor
    from {TXN}
    where owner_id='u1' and type in ('purchase','refund') and status='posted'
      and posted_at_utc >= '2026-08-31T16:00:00+00:00' and posted_at_utc < '2026-09-24T16:00:00+00:00'
    group by 1 order by 1
    """
)

# Previous corresponding month window (Aug-01..Aug-24 Shanghai) for u1
out["u1_net_spending_prev_month"] = rows(
    f"""
    select coalesce(category,'未分类') as category,
           sum(case when type='purchase' and status='posted' then amount_minor else 0 end)
         - sum(case when type='refund'   and status='posted' then amount_minor else 0 end) as net_minor
    from {TXN}
    where owner_id='u1' and type in ('purchase','refund') and status='posted'
      and posted_at_utc >= '2026-07-31T16:00:00+00:00' and posted_at_utc < '2026-08-23T16:00:00+00:00'
    group by 1 order by 1
    """
)

# Last night window: demo_date - 1 day 18:00 .. demo_date 06:00 Shanghai
out["u1_last_night"] = rows(
    f"""
    select id, account_id, card_id, type, status, amount_minor, category, merchant, posted_at_utc
    from {TXN}
    where owner_id='u1'
      and posted_at_utc >= '2026-09-23T10:00:00+00:00' and posted_at_utc < '2026-09-23T22:00:00+00:00'
    order by posted_at_utc
    """
)

safe("coverage", f"select owner_id, account_id, start_utc, end_utc, completeness from {q_ident('coverage')} order by account_id") if TBL["coverage"] else None

out["counts"] = {
    name: (q(f'select count(*) from "{name}"').fetchone()[0] if table_exists(name) else None)
    for name in sorted(out["tables"])
}

print(json.dumps(out, ensure_ascii=False, indent=2))
