"""Add birthday workflow tables; preserve all old columns and account funds."""
from alembic import op
from app.data.models import Base
from app.data import birthday_models  # noqa: F401

revision = "0012_birthday_workflows"
down_revision = "0011_subscription_management"
branch_labels = None
depends_on = None
TABLES = ("birthday_plans", "birthday_requests", "birthday_reservations", "birthday_orders",
          "birthday_fund_events", "birthday_notifications")


def upgrade():
    for name in TABLES:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
