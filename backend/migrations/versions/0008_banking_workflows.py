"""Persist explicitly authorized scheduled transfers and simulated AA collection."""
from alembic import op
from app.data.models import Base
revision = '0008_banking_workflows'
down_revision = '0007_recipient_remark'
branch_labels = None
depends_on = None
TABLES = ('scheduled_transfers', 'collection_requests', 'collection_members', 'collection_payments')


def upgrade():
    for name in TABLES:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
