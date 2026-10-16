"""Persist simulated receiving legs; no retroactive fabrication of old receipts."""
from alembic import op
from app.data.models import RecipientCredit
revision = "0005_recipient_credits"
down_revision = "0004_m5_plans"
branch_labels = None
depends_on = None


def upgrade():
    RecipientCredit.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table("recipient_credits")
