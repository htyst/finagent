"""Permit separately authorized loss reversal without resetting card controls."""
from alembic import op
from sqlalchemy import Table,MetaData,CheckConstraint,text
revision='0016_card_unloss'
down_revision='0015_financial_reads'
branch_labels=None
depends_on=None


def change(allow_unloss):
    connection=op.get_bind()
    table=Table('card_requests',MetaData(),autoload_with=connection)
    checks=[c for c in table.constraints if isinstance(c,CheckConstraint)]
    kind=next(c for c in checks if 'kind IN' in str(c.sqltext))
    # Early migrations create from current metadata; fresh databases may already allow it.
    if ('unloss' in str(kind.sqltext))==allow_unloss:return
    for c in checks:
        if not c.name:c.name='ck_card_requests_kind' if c is kind else 'ck_card_requests_status'
    values="'apply','limit','online_block','online_unblock','loss'"+(",'unloss'" if allow_unloss else '')
    with op.batch_alter_table('card_requests',copy_from=table,recreate='always') as batch:
        batch.drop_constraint(kind.name,type_='check')
        batch.create_check_constraint('ck_card_requests_kind','kind IN ('+values+')')


def upgrade():change(True)


def downgrade():
    if op.get_bind().execute(text("SELECT 1 FROM card_requests WHERE kind='unloss' LIMIT 1")).first():
        raise RuntimeError('Cannot downgrade while loss-reversal requests exist; preserve audit records')
    change(False)
