"""Assert actual available-balance facts, independent of equivalent tool choice."""
from decimal import Decimal
import json
from sqlalchemy import select

def balance_evidence(db,task_id,graph):
 from app.data.models import AgentTask,Account
 from app.domain.ledger import available_balance
 from app.agent.dynamic_runtime import current_evidence
 task=db.get(AgentTask,task_id);r=json.loads(task.context_json)['dynamic_runtime'];active=current_evidence(r)
 expected=Decimal(sum(available_balance(db,a) for a in db.scalars(select(Account).where(Account.owner_id==task.owner_id,Account.status=='active'))))/100
 successful={n['id'] for n in graph['nodes'] if n['status']=='completed' and not n.get('invalidated')}
 for e in active['evidence']:
  if e.get('error') or e['source_ref'].split(':')[-1] not in successful:continue
  fs=[f for f in active['facts'] if f['source_ref']==e['source_ref'] and f['unit']=='yuan']
  if e['tool']=='financial_planning_basis' and any(f['key']=='available_liquid' and Decimal(f['value'])==expected for f in fs):return True
  if e['tool']=='read_financial_profile' and (e.get('raw_data') or {}).get('bank_queried_at_utc') and any(f['key']=='balances.available_minor' and Decimal(f['value'])==expected for f in fs):return True
  if e['tool']=='list_accounts':
   available=[Decimal(f['value']) for f in fs if f['key'].endswith('available_balance_minor')]
   if available and sum(available)==expected:return True
 return False
