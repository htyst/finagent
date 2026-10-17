"""Add independently authorized card, credit and merchant simulator servicing."""
from alembic import op
from app.data.models import Base
from app.data import servicing_models
revision='0017_servicing'
down_revision='0016_card_unloss'
branch_labels=None
depends_on=None
TABLES=('service_requests','credit_facilities','merchant_contract_states')


def upgrade():
    for name in TABLES:Base.metadata.tables[name].create(op.get_bind(),checkfirst=True)


def downgrade():
    from sqlalchemy import text
    if any(op.get_bind().execute(text('SELECT 1 FROM '+name+' LIMIT 1')).first() for name in TABLES):
        raise RuntimeError('Servicing records exist; preserve approvals, receipts and contract states')
    for name in reversed(TABLES):Base.metadata.tables[name].drop(op.get_bind(),checkfirst=True)
