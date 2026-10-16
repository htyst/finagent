"""Only the isolated startup regression fixture may be downgraded."""
import json
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from app.config import ROOT, database_path
from app.data.db import make_engine

path = database_path().resolve()
relative = path.relative_to((ROOT / "var").resolve())
assert relative.parts[0].startswith("startup-review-") and path.name == "finagent.sqlite3"
assert sys.argv[1] in {"legacy", "snapshot"}
if sys.argv[1] == "legacy":
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    command.downgrade(config, "0002_m3_operations")
engine = make_engine(path)
try:
    with engine.connect() as db:
        if sys.argv[1] == "snapshot":
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004_m5_plans"
            assert db.execute(text("SELECT count(*) FROM agent_tasks")).scalar() == 0
        print(json.dumps({"dataset":db.execute(text("SELECT dataset_version FROM demo_settings")).scalar(),
                          "balances":[list(row) for row in db.execute(text("SELECT id, balance_minor FROM accounts ORDER BY id"))]}))
finally:
    engine.dispose()
