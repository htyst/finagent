import json
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.config import database_path
from app.data.db import make_engine
from app.data.models import BusinessResult, Account, Card
path = database_path().resolve()
assert path.parent.name == "var" and path.name in {"m7-process-mock.sqlite3", "m7-process-live.sqlite3"}
with Session(make_engine(path)) as db:
    print(json.dumps({"business_results": db.scalar(select(func.count()).select_from(BusinessResult)),
                      "source_balance": db.get(Account, "a1c").balance_minor,
                      "savings_balance": db.get(Account, "a1s").balance_minor,
                      "card": db.get(Card, "c1a").status}))
