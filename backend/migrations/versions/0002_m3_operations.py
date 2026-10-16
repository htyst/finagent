"""M3 proposals, business receipts, audit events and session CSRF.

Revision ID: 0002_m3_operations
Revises: 0001_initial
"""

from alembic import op
from sqlalchemy import Column, String, inspect

from app.data.models import AuditEvent, Base, BusinessResult, CardActionOrder, Proposal, Task, TransferOrder

revision = "0002_m3_operations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

M3_TABLES = (Task.__table__, Proposal.__table__, TransferOrder.__table__,
             CardActionOrder.__table__, BusinessResult.__table__, AuditEvent.__table__)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("demo_sessions")}
    if "csrf_token" not in columns:
        op.add_column("demo_sessions", Column("csrf_token", String(), nullable=False, server_default=""))
    Base.metadata.create_all(bind=bind, tables=M3_TABLES, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=list(reversed(M3_TABLES)), checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("demo_sessions")}
    if "csrf_token" in columns:
        op.drop_column("demo_sessions", "csrf_token")
