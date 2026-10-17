"""Indexed financial reads and explicit payment-card binding; no cash changes."""
import json
from datetime import datetime,timezone
from alembic import op
from sqlalchemy import inspect,text,Column,String
from app.data.models import Base
from app.data import financial_models
revision='0015_financial_reads'
down_revision='0014_financial_dataset'
branch_labels=None
depends_on=None

def upgrade():
    connection=op.get_bind()
    Base.metadata.tables['financial_events'].create(connection,checkfirst=True)
    columns={c['name'] for c in inspect(connection).get_columns('subscription_authorizations')}
    if 'payment_card_id' not in columns:op.add_column('subscription_authorizations',Column('payment_card_id',String(),nullable=True))
    for name,columns in [('ix_transactions_owner_date','owner_id, posted_at_utc'),('ix_transactions_owner_category_date','owner_id, category, posted_at_utc'),('ix_transactions_card_date','card_id, posted_at_utc')]:
        connection.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON transactions ({columns})'))
    # Historical initial migrations use today's metadata; this remains idempotent.
    records=connection.execute(text("SELECT dataset_version,owner_id,payload_json FROM financial_dataset_rows WHERE table_name='liability_events'"))
    for version,owner,payload in records:
        r=json.loads(payload)
        connection.execute(text('INSERT OR IGNORE INTO financial_events (dataset_version,id,owner_id,card_id,liability_id,event_type,amount_minor,debt_delta_minor,category,merchant,posted_at_utc,subscription_id,cash_transaction_id,business_id) VALUES (:version,:id,:owner,:card,:liability,:kind,:amount,:delta,:category,:merchant,:at,:subscription,:cash,:business)'),
            dict(version=version,id=r['event_id'],owner=owner,card=r.get('card_id') or None,liability=r['liability_id'],kind=r['event_type'],amount=int(r['amount_minor']),delta=int(r['debt_delta_minor']),category=r.get('category') or None,merchant=r.get('merchant') or None,
                at=datetime.fromisoformat(r['occurred_at_local']).astimezone(timezone.utc).isoformat(timespec='seconds'),subscription=r.get('subscription_id') or None,cash=r.get('cash_transaction_id') or None,business=r.get('business_id') or None))
    contracts={}
    for version,payload in connection.execute(text("SELECT dataset_version,payload_json FROM financial_dataset_rows WHERE table_name='subscriptions'")):
        r=json.loads(payload);contracts[(version,r['subscription_id'])]=r
    for version,identifier,payload in connection.execute(text("SELECT dataset_version,row_key,payload_json FROM financial_dataset_rows WHERE table_name='native_subscription_links'")):
        r=contracts.get((version,json.loads(payload)['contract_id']))
        if r:connection.execute(text('UPDATE subscription_authorizations SET payment_card_id=:card WHERE id=:id AND dataset_version=:version'),dict(card=r['payment_card_id'],id=identifier,version=version))

def downgrade():
    for name in ('ix_transactions_owner_date','ix_transactions_owner_category_date','ix_transactions_card_date'):op.drop_index(name,table_name='transactions')
    with op.batch_alter_table('subscription_authorizations') as batch:batch.drop_column('payment_card_id')
    op.drop_table('financial_events')
