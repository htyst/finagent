"""Read-only calibration summary; no business writes and no model calls."""
import argparse,json,sqlite3
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--db',type=Path,required=True)
args=parser.parse_args()
connection=sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True)
hits={}
for rid,phase,payload in connection.execute('SELECT id,phase,payload_json FROM verification_records'):
    for violation in json.loads(payload).get('would_block',[]):
        hits.setdefault(violation['rule_id'],[]).append({'record_id':rid,'phase':phase,'field':violation['field']})
print(json.dumps({rule:{'count':len(items),'cases':items} for rule,items in hits.items()},ensure_ascii=False,indent=2))
