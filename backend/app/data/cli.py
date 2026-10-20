from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.config import ROOT, database_path
from app.data.db import make_engine
from app.data.seed import SEED_VERSION, check_ledger, reset_demo


def project_database() -> Path:
    path = database_path().resolve()
    base = (ROOT / "var").resolve()
    if path != base / "finagent.sqlite3" and base not in path.parents:
        raise SystemExit("拒绝操作项目 var/ 之外的数据库")
    return path


def migrate() -> None:
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["reset", "check", "migrate"])
    parser.add_argument("--date", default="2026-09-24")
    args = parser.parse_args()
    path = project_database()
    os.environ["FINAGENT_DB_PATH"] = str(path)
    if args.action == "reset":
        selected = date.fromisoformat(args.date)
        path.parent.mkdir(parents=True, exist_ok=True)
        migrate()
        with Session(make_engine(path)) as db:
            version = reset_demo(db, selected)
            errors = check_ledger(db)
            if errors:
                raise SystemExit(f"账本核对失败: {errors}")
            print(f"已重置合成数据：日期={selected}，种子版本={SEED_VERSION}，数据版本={version}")
    else:
        if not path.exists():
            raise SystemExit("数据库不存在；首次使用请运行 scripts/initialize-demo.py（完整年度合成数据）")
        if args.action == "migrate":
            migrate()
            print("数据库结构升级完成，保留已有演示数据")
            return
        with Session(make_engine(path)) as db:
            errors = check_ledger(db)
            if errors:
                raise SystemExit(f"账本核对失败: {errors}")
            print("账本核对通过")


if __name__ == "__main__":
    main()
