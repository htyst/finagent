"""Fact-only task correlation; no messages, provider reasoning, OTPs or tokens."""
from hashlib import sha256
from uuid import uuid4
import json
from datetime import datetime,timezone

def event(r,task,kind,node=None,**extra):
    n=node or {}
    record=dict(event_id='evt-'+uuid4().hex,event=kind,task_id=task.id,
        request_id=r.get('request_id'),runtime_id=r['id'],plan_version=task.plan_version,
        node_id=n.get('id'),goal_ids=n.get('goal_ids',[]),tool=n.get('name'),
        parameter_sha256=sha256(json.dumps(n.get('arguments',{}),sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        evidence_refs=n.get('result_refs',[]),resource_ref=n.get('resource_ref'),receipt_refs=n.get('receipt_refs',[]),
        status=n.get('status'),at_utc=datetime.now(timezone.utc).isoformat(),**extra)
    r.setdefault('events',[]).append(record)
    # Trace horizon is bounded; authoritative domain audits and receipts survive.
    r['events']=r['events'][-256:]
    return record
