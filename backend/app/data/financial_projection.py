"""Rebuildable read indexes. Importing a projection never posts money."""
import json
from datetime import datetime,timezone
from sqlalchemy import select,delete,insert
from app.data.financial_models import FinancialDatasetRow,FinancialEvent

def rebuild_events(db,version):
    db.execute(delete(FinancialEvent).where(FinancialEvent.dataset_version==version))
    values=[]
    for row in db.scalars(select(FinancialDatasetRow).where(FinancialDatasetRow.dataset_version==version,FinancialDatasetRow.table_name=='liability_events')):
        r=json.loads(row.payload_json)
        values.append(dict(dataset_version=version,id=r['event_id'],owner_id=row.owner_id,card_id=r.get('card_id') or None,
            liability_id=r['liability_id'],event_type=r['event_type'],amount_minor=int(r['amount_minor']),debt_delta_minor=int(r['debt_delta_minor']),
            category=r.get('category') or None,merchant=r.get('merchant') or None,
            posted_at_utc=datetime.fromisoformat(r['occurred_at_local']).astimezone(timezone.utc).isoformat(timespec='seconds'),
            subscription_id=r.get('subscription_id') or None,cash_transaction_id=r.get('cash_transaction_id') or None,business_id=r.get('business_id') or None))
    for start in range(0,len(values),300):db.execute(insert(FinancialEvent),values[start:start+300])
    return len(values)
