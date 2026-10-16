"""Composable SQLite write transactions for accepting an agent response atomically."""
from contextlib import contextmanager


def commit(db):
    if db.info.get("atomic_write"):
        db.flush()
    else:
        db.commit()


@contextmanager
def atomic_write(db):
    if db.info.get("atomic_write"):
        with db.begin_nested():
            yield
        return
    # Callers must persist their request before entering. Rollback expires cached
    # ORM objects, including sessions created with expire_on_commit=False.
    db.rollback()
    # rollback() is a no-op when no transaction is active; explicitly discard
    # cached state after a previous commit with expire_on_commit=False.
    db.expire_all()
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    db.info["atomic_write"] = True
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.info.pop("atomic_write", None)
