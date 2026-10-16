"""Add a second candidate to the isolated browser fixture."""
from datetime import datetime
from sqlalchemy.orm import Session
from app.config import database_path
from app.data.db import make_engine
from app.domain.ledger import post_entry
path = database_path()
assert path.name.startswith("m5-browser-") and path.parent.name == "var"
with Session(make_engine(path)) as db:
    tx = post_entry(db, owner_id="u1", account_id="a1s", kind="purchase", direction="debit",
                   amount_minor=9900, card_id="c1b", business_at=datetime.fromisoformat("2026-09-23T22:00:00+08:00"))
    tx.merchant = "夜间书店"
    db.commit()
