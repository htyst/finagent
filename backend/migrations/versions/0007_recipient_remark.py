"""Persist owner-maintained recipient remarks without inferring historical values."""
from alembic import op
import sqlalchemy as sa
revision = '0007_recipient_remark'
down_revision = '0006_recipient_phone'
branch_labels = None
depends_on = None


def upgrade():
    if 'remark' not in {item['name'] for item in sa.inspect(op.get_bind()).get_columns('recipients')}:
        op.add_column('recipients', sa.Column('remark', sa.String(80), nullable=True))


def downgrade():
    op.drop_column('recipients', 'remark')
