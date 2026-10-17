"""Preserve complete annual financial profiles without granting execution rights."""
from alembic import op
from app.data.models import Base
from app.data import financial_models
revision='0014_financial_dataset'
down_revision='0013_security'
branch_labels=None
depends_on=None
def upgrade():
    for name in ('financial_dataset_imports','financial_dataset_rows'):
        Base.metadata.tables[name].create(op.get_bind(),checkfirst=True)
def downgrade():
    for name in ('financial_dataset_rows','financial_dataset_imports'):
        Base.metadata.tables[name].drop(op.get_bind(),checkfirst=True)
