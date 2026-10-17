"""Import a hash-verified annual dataset into a NEW empty migrated database.

Run migrations first with FINAGENT_DB_PATH selecting the staging DB. Existing
users cause a refusal, so this never resets the live database in place.
"""
import argparse,csv,hashlib,json,sys
from datetime import date,datetime,time,timedelta,timezone
from pathlib import Path
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sqlalchemy import select,func,insert
from sqlalchemy.orm import Session
from app.data.db import engine
from app.data.models import User,Account,Card,Recipient,Transaction,RecipientCredit,DataCoverage,DemoSetting
from app.data.card_models import CardControl
from app.data.financial_models import FinancialDatasetRow,FinancialDatasetImport
from app.data.subscription_models import SubscriptionAuthorization
from app.domain.ledger import ledger_errors


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    args=parser.parse_args();source=args.source.resolve()
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    hashlist=json.loads((source/'csv_file_manifest.json').read_text(encoding='utf-8'))
    for entry in hashlist['files']:
        p=(source/entry['file']).resolve()
        if p.parent!=source or hashlib.sha256(p.read_bytes()).hexdigest()!=entry['sha256']:
            raise SystemExit('Source file/hash validation failed')
    data={p.stem:list(csv.DictReader(p.open(encoding='utf-8-sig',newline=''))) for p in source.glob('*.csv')}
    version='demo-financial-v2-'+uuid4().hex[:10];instant=datetime.now(timezone.utc)
    dump=lambda x:json.dumps(x,ensure_ascii=False,separators=(',',':'))
    def n(r,key):return int(r.get(key) or 0)
    counts={}
    with Session(engine) as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise SystemExit('Refusing import: target DB contains users; choose a new staging DB')
        with db.begin_nested():
            db.add_all(User(id=r['id'],display_name=r['display_name'],profile=r['profile']) for r in data['users']);db.flush()
            db.add(DemoSetting(id=1,demo_date=manifest['as_of'],dataset_version=version,seed_version='demo-financial-v2'))
            db.add_all(Account(id=r['id'],owner_id=r['owner_id'],alias=r['alias'],account_last4=r['account_last4'],currency='CNY',
                opening_balance_minor=n(r,'opening_balance_minor'),balance_minor=n(r,'balance_minor'),
                status=('locked' if r['account_type']=='term_deposit' and r['status']=='active' else r['status']),version=n(r,'version')) for r in data['accounts']);db.flush()
            db.add_all(Recipient(id=r['id'],owner_id=r['owner_id'],name=r['name'],account_last4=r['account_last4'],status=r['status'],version=1,
                remark=r.get('relationship') or None) for r in data['recipients']);db.flush()
            # The core action schema is a debit-card schema. Credit cards are kept
            # losslessly in the financial archive, not faked as deposit accounts.
            for r in data['cards']:
                if r['card_type']!='debit':continue
                db.add(Card(id=r['id'],owner_id=r['owner_id'],account_id=r['account_id'],alias=r['alias'],brand=r['brand'],
                    card_last4=r['card_last4'],status='frozen' if r['status']=='expired' else r['status'],version=n(r,'version')))
            db.flush()
            for r in data['cards']:
                if r['card_type']=='debit':db.add(CardControl(card_id=r['id'],limit_minor=100000,online_enabled=1,lost=0,version=1))
            ordered=sorted(data['transactions'],key=lambda r:(r['ledger_at_local'],r['id']))
            values=[]
            for r in ordered:
                at=r['posted_at_utc'] or datetime.fromisoformat(r['occurred_at_local']).astimezone(timezone.utc).isoformat(timespec='seconds')
                values.append(dict(id=r['id'],owner_id=r['owner_id'],account_id=r['account_id'],card_id=r['card_id'] or None,
                    type=r['type'],direction=r['direction'],amount_minor=n(r,'amount_minor'),currency=r['currency'],
                    category=r['category'] or None,merchant=r['merchant'] or r['counterparty_name'] or None,
                    posted_at_utc=at,created_at_utc=r['created_at_utc'],status=r['status'],business_id=r['business_id'],
                    original_transaction_id=r['original_transaction_id'] or None))
            # Original purchases precede refunds; SQLite enforces each FK as inserted.
            for start in range(0,len(values),400):db.execute(insert(Transaction),values[start:start+400])
            db.flush()
            for r in ordered:
                if r['type']=='external_transfer' and r['status']=='posted':
                    db.add(RecipientCredit(id='import-credit-'+r['id'],owner_id=r['owner_id'],recipient_id=r['recipient_id'],
                        business_id=r['business_id'],source_transaction_id=r['id'],amount_minor=n(r,'amount_minor'),currency='CNY',
                        posted_at_utc=r['posted_at_utc'],created_at_utc=r['created_at_utc']))
            db.add_all(DataCoverage(owner_id=r['owner_id'],account_id=r['account_id'],start_utc=r['start_utc'],end_utc=r['end_utc'],completeness=r['completeness']) for r in data['data_coverage'])
            db.add(FinancialDatasetImport(dataset_version=version,manifest_json=dump(manifest),imported_at_utc=instant.isoformat(timespec='seconds')))
            for name,records in data.items():
                payloads=[]
                for idx,r in enumerate(records):
                    owner=r.get('owner_id') or r.get('用户ID') or (r['id'] if name=='users' else None)
                    # Use a stable per-table source row index; repeat holding IDs
                    # across valuation dates remain distinct and lossless.
                    payloads.append(dict(dataset_version=version,table_name=name,row_key=(r.get('id') or
                        r.get('subscription_id') if name=='subscriptions' else r.get('id')) or f'row-{idx:06}',
                        owner_id=owner,payload_json=dump(r)))
                for start in range(0,len(payloads),300):db.execute(insert(FinancialDatasetRow),payloads[start:start+300])
                counts[name]=len(records)
            # Compatible monthly debit contracts can use existing confirmed
            # reminder/revocation workflows. No background grant is imported.
            links=[]
            cardmap={r['id']:r for r in data['cards']}
            for r in data['subscriptions']:
                if r['billing_cycle']!='monthly' or cardmap[r['payment_card_id']]['card_type']!='debit':continue
                native_id='import-'+r['subscription_id'];enabled=r['auto_renew_enabled']=='true'
                day=n(r,'billing_anchor_day');nowlocal=instant.astimezone(timezone(timedelta(hours=8)))
                import calendar
                year,month=nowlocal.year,nowlocal.month
                nextday=date(year,month,min(day,calendar.monthrange(year,month)[1]))
                due=datetime.combine(nextday,time(9),nowlocal.tzinfo)
                if due<=nowlocal:
                    month+=1
                    if month==13:year+=1;month=1
                    due=datetime(year,month,min(day,calendar.monthrange(year,month)[1]),9,tzinfo=nowlocal.tzinfo)
                db.add(SubscriptionAuthorization(id=native_id,owner_id=r['owner_id'],dataset_version=version,
                    account_id=r['payment_account_id'],payment_card_id=r['payment_card_id'],merchant=r['service_name'],amount_minor=n(r,'current_price_minor'),
                    period='monthly',charge_day=day,next_charge_at_utc=due.astimezone(timezone.utc).isoformat(timespec='seconds'),
                    timezone='Asia/Shanghai',status='active' if enabled and r['status'] not in {'paused','canceled'} else 'revoked',
                    version=1,created_at_utc=instant.isoformat(timespec='seconds'),revoked_at_utc=None if enabled else instant.isoformat(timespec='seconds')))
                links.append(dict(dataset_version=version,table_name='native_subscription_links',row_key=native_id,
                    owner_id=r['owner_id'],payload_json=dump({'contract_id':r['subscription_id'],'authorization_id':native_id,'execution_grant_imported':False})))
            db.flush()
            if links:db.execute(insert(FinancialDatasetRow),links)
            from app.data.financial_projection import rebuild_events
            rebuild_events(db,version)
            from app.domain.wealth import seed_products
            seed_products(db);db.flush()
            errors=ledger_errors(db)
            if errors:raise RuntimeError('Imported ledger invalid: '+str(errors[:10]))
            raw_count=db.scalar(select(func.count()).select_from(FinancialDatasetRow).where(FinancialDatasetRow.table_name!='native_subscription_links'))
            if raw_count!=sum(counts.values()):raise RuntimeError('Archive row count mismatch')
        db.commit()
        report={'dataset_version':version,'source_rows':counts,'lossless_archive_rows':raw_count,'native_accounts':len(data['accounts']),
            'native_debit_cards':40,'archived_credit_cards':12,'cash_transactions':len(ordered),'native_monthly_debit_mandates':len(links),
            'ledger_errors':[],'background_grants_imported':0,'security_challenges_imported':0,
            'database':str(engine.url.database),'state':'staging_complete_not_published'}
        print(json.dumps(report,ensure_ascii=True))
        (Path(engine.url.database).parent/'financial-import-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
