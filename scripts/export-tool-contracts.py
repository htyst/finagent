"""Export implementation-derived contracts, without importing secrets into output."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from app.agent.tool_contracts import generate
out=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'docs/evidence/current-tool-contracts.json'
if not out.is_absolute():out=ROOT/out
out.parent.mkdir(parents=True,exist_ok=True);data=generate();out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:data[k] for k in ['contract_version','tool_count','model_tool_count','sha256']}))
