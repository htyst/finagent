"""Add subscription tables without changing existing columns, data or mandates."""
from alembic import op
from app.data.models import Base
from app.data import subscription_models  # noqa: F401 -- register tables

revision = "0011_subscription_management"
down_revision = "0010_card_management"
branch_labels = None
depends_on = None
TABLES = ("subscription_authorizations", "subscription_requests", "subscription_reminders",
          "subscription_notifications", "subscription_charges")


def upgrade():
    for name in TABLES:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
