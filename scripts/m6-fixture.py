"""Controlled faults and database assertions for the disposable M6 browser DB only."""
import json,sys
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select,delete,func
from sqlalchemy.orm import Session
from app.config import database_path
from app.data.db import make_engine
from app.data.models import Account,Card,Proposal,TransferOrder,CardActionOrder,BusinessResult,DataCoverage
from app.data.seed import reset_demo
from app.domain.ledger import post_refund
path=database_path().resolve()
assert path.parent.name == "var" and path.name.startswith("m6-browser-"), "Fixture refused non-test database"
with Session(make_engine(path)) as db:
    action=sys.argv[1]
    if action == "reset": reset_demo(db,date(2026,9,24))
    elif action == "expire":
        for proposal in db.scalars(select(Proposal).where(Proposal.status == "pending_confirmation")):
            proposal.expires_at_utc=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        db.commit()
    elif action == "refund":
        post_refund(db,owner_id="u1",original_id="t_food_1000",amount_minor=80000,business_at=datetime.fromisoformat("2026-09-24T12:00:00+08:00"));db.commit()
    elif action == "incomplete":
        db.execute(delete(DataCoverage).where(DataCoverage.owner_id == "u1"));db.commit()
    elif action == "snapshot":
        print(json.dumps({"balances":{row.id:row.balance_minor for row in db.scalars(select(Account))},
            "cards":{row.id:row.status for row in db.scalars(select(Card))},
            "transfers":db.scalar(select(func.count()).select_from(TransferOrder)),
            "card_orders":db.scalar(select(func.count()).select_from(CardActionOrder)),
            "results":db.scalar(select(func.count()).select_from(BusinessResult))}))
    else: raise ValueError("Unknown test fixture action")
